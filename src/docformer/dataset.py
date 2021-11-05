# -*- coding: utf-8 -*-
import os
import pickle
import pytesseract
import numpy as np
from PIL import Image
import torch
from torchvision.transforms import ToTensor


def normalize_box(box, width, height, size=1000):
    """
    Takes a bounding box and normalizes it to a thousand pixels. If you notice it is
    just like calculating percentage except takes 1000 instead of 100.
    """
    return [
        int(size * (box[0] / width)),
        int(size * (box[1] / height)),
        int(size * (box[2] / width)),
        int(size * (box[3] / height)),
    ]


def resize_align_bbox(bbox, original_image, target_size):
    x_, y_ = original_image.size
    x_scale = target_size / x_
    y_scale = target_size / y_
    orig_left, orig_top, orig_right, orig_bottom = tuple(bbox)
    x = int(np.round(orig_left * x_scale))
    y = int(np.round(orig_top * y_scale))
    xmax = int(np.round(orig_right * x_scale))
    ymax = int(np.round(orig_bottom * y_scale))
    return [x, y, xmax, ymax]


def get_topleft_bottomright_coordinates(df_row):
    left, top, width, height = df_row["left"], df_row["top"], df_row["width"], df_row["height"]
    return [left, top, left + width, top + height]


def apply_ocr(image_fp):
    """
    Returns words and its bounding boxes from an image
    """
    image = Image.open(image_fp)
    width, height = image.size

    ocr_df = pytesseract.image_to_data(image, output_type="data.frame")
    ocr_df = ocr_df.dropna().reset_index(drop=True)
    float_cols = ocr_df.select_dtypes("float").columns
    ocr_df[float_cols] = ocr_df[float_cols].round(0).astype(int)
    ocr_df = ocr_df.replace(r"^\s*$", np.nan, regex=True)
    ocr_df = ocr_df.dropna().reset_index(drop=True)
    words = list(ocr_df.text.apply(str))
    actual_bboxes = ocr_df.apply(get_topleft_bottomright_coordinates, axis=1).tolist()

    # add as extra columns
    assert len(words) == len(actual_bboxes)
    return {"words": words, "bbox": actual_bboxes}


def get_tokens_with_bboxes(tokenizer,
                           words,
                           unnormalized_word_boxes,
                           normalized_word_boxes):

    normalized_token_boxes = []
    unnormalized_token_boxes = []
    trio = zip(words, unnormalized_word_boxes, normalized_word_boxes)

    for word, unnormalized_box, normalized_box in trio:
        word_tokens = tokenizer.tokenize(word)
        unnormalized_token_boxes.extend(
            unnormalized_box for _ in range(len(word_tokens))
        )
        normalized_token_boxes.extend(normalized_box for _ in range(len(word_tokens)))

    return (
        normalized_token_boxes,
        unnormalized_token_boxes,
    )


def get_centroid(actual_bbox):
    centroid = []
    for i in actual_bbox:
        width = i[2] - i[0]
        height = i[3] - i[1]
        centroid.append([i[0] + width / 2, i[1] + height / 2])
    return centroid


def get_pad_token_id_start_index(words, encoding, tokenizer): 
#     assert len(words) < len(encoding["input_ids"])  This condition, was creating errors on some sample images
    for idx in range(len(encoding["input_ids"])):
        if encoding["input_ids"][idx] == tokenizer.pad_token_id:
            break
    return idx


def get_relative_distance(bboxes, centroids, pad_tokens_start_idx):
    """
    Note: We take absolute distances to avoid embedding problems.
    Reason is that all we want to know is how FAR one word is
    from the other (in x-dim). A change of line will be reflected
    in the y-dim.
    """
    a_rel_x = []
    a_rel_y = []
    a_rel_x.append([0] * 8)  # For the "first" CLS token
    a_rel_y.append([0] * 8)  # For the "first" CLS token

    for i in range(1, len(bboxes)):
        if i > pad_tokens_start_idx:
            a_rel_x.append([0] * 8)
            a_rel_y.append([0] * 8)
            continue

        curr = bboxes[i]
        prev = bboxes[i - 1]
        a_rel_x.append(
            [
                curr[0],  # top left x
                curr[2],  # bottom right x
                abs(curr[2] - curr[0]),  # width
                abs(curr[0] - prev[0]),  # diff top left x
                abs(curr[0] - prev[0]),  # diff bottom left x
                abs(curr[2] - prev[2]),  # diff top right x
                abs(curr[2] - prev[2]),  # diff bottom right x
                abs(centroids[i][0] - centroids[i - 1][0]),
            ]
        )
        a_rel_y.append(
            [
                curr[1],  # top left y
                curr[3],  # bottom right y
                abs(curr[3] - curr[1]),  # height
                abs(curr[1] - prev[1]),  # diff top left y
                abs(curr[3] - prev[3]),  # diff bottom left y
                abs(curr[1] - prev[1]),  # diff top right y
                abs(curr[3] - prev[3]),  # diff bottom right y
                abs(centroids[i][1] - centroids[i - 1][1]),
            ]
        )

    return a_rel_x, a_rel_y


def apply_mask(inputs, tokenizer):
    inputs = torch.as_tensor(inputs)
    rand = torch.rand(inputs.shape)
    # where the random array is less than 0.15, we set true
    mask_arr = (rand < 0.15) * (inputs != tokenizer.cls_token_id) * (inputs != tokenizer.pad_token_id)
    # create selection from mask_arr
    selection = torch.flatten(mask_arr.nonzero()).tolist()
    # apply selection pad_tokens_start_idx to inputs.input_ids, adding MASK tokens
    inputs[selection] = 103
    return inputs


def read_image_and_extract_text(image):
    original_image = Image.open(image).convert("RGB")
    return apply_ocr(image)


def create_features(
        image,
        tokenizer,
        add_batch_dim=True,
        target_size=224,
        max_seq_length=512,
        path_to_save=None,
        save_to_disk=False,
        apply_mask_for_mlm=False,
        extras_for_debugging=False,
):

    pad_token_box = [0, 0, 0, 0]

    # step 1: read original image and extract OCR entries
    original_image = Image.open(image).convert("RGB")
    entries = apply_ocr(image)

    # step 2: resize image
    resized_image = original_image.resize((target_size, target_size))
    unnormalized_word_boxes = entries["bbox"]
    words = entries["words"]

    # step 3: normalize image to a grid of 1000 x 1000 (to avoid the problem of differently sized images)
    width, height = original_image.size
    normalized_word_boxes = [
        normalize_box(bbox, width, height, 1000) for bbox in unnormalized_word_boxes
    ]
    assert len(words) == len(normalized_word_boxes), "Length of words != Length of normalized words"

    # step 4: tokenize words and get their bounding boxes (one word may split into multiple tokens)
    token_boxes, unnormalized_token_boxes = get_tokens_with_bboxes(
        tokenizer, words, unnormalized_word_boxes, normalized_word_boxes
    )

    # step 5: add special tokens and truncate seq. to maximum length
    special_tokens_count = 1
    remaining_length = max_seq_length - special_tokens_count
    if len(token_boxes) > remaining_length:
        token_boxes = token_boxes[: remaining_length]
        unnormalized_token_boxes = unnormalized_token_boxes[: remaining_length]

    token_boxes = [[0, 0, 0, 0]] + token_boxes
    unnormalized_token_boxes = ([[0, 0, 0, 0]] + unnormalized_token_boxes)
    encoding = tokenizer(words,
                         padding="max_length",
                         max_length=max_seq_length,
                         is_split_into_words=True,
                         truncation=True,
                         add_special_tokens=False)
    # add CLS token manually to avoid autom. addition of SEP too (as in the paper)
    encoding["input_ids"] = [tokenizer.cls_token_id] + encoding["input_ids"][:-1]

    # step 6: pad token_boxes up to the sequence length
    assert len(encoding["input_ids"]) == len(token_boxes), "Length of input ids != Length of token boxes"  # check if number of tokens match
    padding_length = max_seq_length - len(encoding["input_ids"])
    token_boxes += [pad_token_box] * padding_length
    unnormalized_token_boxes += [pad_token_box] * padding_length
    encoding["bbox"] = token_boxes
    encoding["unnormalized_token_boxes"] = unnormalized_token_boxes
   
    # step 7: apply mask for the sake of pre-training
    if apply_mask_for_mlm:
        encoding["mlm_labels"] = encoding["input_ids"]
        encoding["input_ids"] = apply_mask(encoding["input_ids"], tokenizer)
        assert len(encoding["mlm_labels"]) == max_seq_length , "Length of mlm_labels != Length of max_seq_length"
       
    assert len(encoding["input_ids"]) == max_seq_length,"Length of input_ids != Length of max_seq_length"
    assert len(encoding["attention_mask"]) == max_seq_length,"Length of attention mask != Length of max_seq_length"
    assert len(encoding["token_type_ids"]) == max_seq_length, "Length of token type ids != Length of max_seq_length"
    assert len(encoding["bbox"]) == max_seq_length, "Length of bbox != Length of max_seq_length"

    # step 8: normalize the image
    encoding["resized_scaled_img"] = ToTensor()(resized_image) / 255.0

    # step 9: apply mask for the sake of pre-training
    if apply_mask_for_mlm:
        encoding["mlm_labels"] = encoding["input_ids"]
        encoding["input_ids"] = apply_mask(encoding["input_ids"], tokenizer)

    # step 10: rescale and align the bounding boxes to match the resized image size (typically 224x224)
    resized_and_aligned_bboxes = []
    for bbox in unnormalized_token_boxes:
        resized_and_aligned_bboxes.append(resize_align_bbox(bbox, original_image, target_size))
    encoding["resized_and_aligned_bounding_boxes"] = resized_and_aligned_bboxes
    
    # step 11: add the relative distances in the normalized grid
    bboxes_centroids = get_centroid(resized_and_aligned_bboxes)
    pad_token_start_index = get_pad_token_id_start_index(words, encoding, tokenizer)
    a_rel_x, a_rel_y = get_relative_distance(resized_and_aligned_bboxes, bboxes_centroids, pad_token_start_index)

    # step 12: convert all to tensors
    for k, v in encoding.items():
        encoding[k] = torch.as_tensor(encoding[k])

    encoding.update({
        "x_features": torch.as_tensor(a_rel_x, dtype=torch.int32),
        "y_features": torch.as_tensor(a_rel_y, dtype=torch.int32),
        })
    assert torch.lt(encoding["x_features"], 0).sum().item() == 0
    assert torch.lt(encoding["y_features"], 0).sum().item() == 0

    # step 13: add tokens for debugging
    if extras_for_debugging:
        input_ids = encoding["mlm_labels"] if apply_mask_for_mlm else encoding["input_ids"]
        encoding["tokens_without_padding"] = ["[CLS]"] + tokenizer.convert_ids_to_tokens(input_ids)
        encoding["words"] = words

    # step 14: add extra dim for batch
    if add_batch_dim:
        encoding["x_features"].unsqueeze_(0)
        encoding["y_features"].unsqueeze_(0)
        encoding["input_ids"].unsqueeze_(0)
        encoding["resized_scaled_img"].unsqueeze_(0)

    # step 15: save to disk
    if save_to_disk:
        os.makedirs(path_to_save, exist_ok=True)
        image_name = os.path.basename(image)
        with open(f"{path_to_save}{image_name}.pickle", "wb") as f:
            pickle.dump(encoding, f)

    return encoding


if __name__ == '__main__':
    from transformers import BertTokenizer
    tokenizer = BertTokenizer.from_pretrained("bert-base-uncased")
    fp = '/media/shabie/S&G/docformer/data/rvl-cdip/images/imagesa/a/a/a/aaa06d00/50486482-6482.tif'
    encoding = create_features(fp, tokenizer)
