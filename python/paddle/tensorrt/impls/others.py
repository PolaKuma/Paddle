# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

import numpy as np
import tensorrt as trt

from paddle.base.log_helper import get_logger
from paddle.tensorrt.converter_utils import (
    add_1D_constant_layer,
    fill_constant_layer,
    get_shape_tensor_element,
    get_trt_plugin,
    trt_concat,
    trt_prod,
    trt_shape,
    trt_sub,
    trt_sum,
)
from paddle.tensorrt.register import converter_registry

_logger = get_logger(
    __name__, logging.INFO, fmt='%(asctime)s-%(levelname)s: %(message)s'
)


@converter_registry.register("pd_op.multiclass_nms3", trt_version="8.x")
def multiclass_nms3_converter(network, paddle_op, inputs):
    bboxes = inputs[0]
    scores = inputs[1]
    background_label = paddle_op.attrs().get("background_label")
    score_threshold = paddle_op.attrs().get("score_threshold")
    nms_top_k = paddle_op.attrs().get("nms_top_k")
    nms_threshold = paddle_op.attrs().get("nms_threshold")
    keep_top_k = paddle_op.attrs().get("keep_top_k")
    normalized = paddle_op.attrs().get("normalized")
    num_classes = scores.shape[1]

    bboxes_dims = bboxes.shape
    bboxes_expand_dims = [bboxes_dims[0], bboxes_dims[1], 1, bboxes_dims[2]]
    bboxes_expand_layer = network.add_shuffle(bboxes)
    bboxes_expand_layer.reshape_dims = trt.Dims(bboxes_expand_dims)

    scores_transpose_layer = network.add_shuffle(scores)
    scores_transpose_layer.first_transpose = (0, 2, 1)

    # create multiclass num3 plugin
    batch_nms_inputs = [
        bboxes_expand_layer.get_output(0),
        scores_transpose_layer.get_output(0),
    ]
    plugin_fields = [
        trt.PluginField(
            "shareLocation",
            np.array([1], dtype=np.int32),
            trt.PluginFieldType.INT32,
        ),
        trt.PluginField(
            "backgroundLabelId",
            np.array(background_label, dtype=np.int32),
            trt.PluginFieldType.INT32,
        ),
        trt.PluginField(
            "numClasses",
            np.array(num_classes, dtype=np.int32),
            trt.PluginFieldType.INT32,
        ),
        trt.PluginField(
            "topK",
            np.array(nms_top_k, dtype=np.int32),
            trt.PluginFieldType.INT32,
        ),
        trt.PluginField(
            "keepTopK",
            np.array(keep_top_k, dtype=np.int32),
            trt.PluginFieldType.INT32,
        ),
        trt.PluginField(
            "scoreThreshold",
            np.array(score_threshold, dtype=np.float32),
            trt.PluginFieldType.FLOAT32,
        ),
        trt.PluginField(
            "iouThreshold",
            np.array(nms_threshold, dtype=np.float32),
            trt.PluginFieldType.FLOAT32,
        ),
        trt.PluginField(
            "isNormalized",
            np.array(normalized, dtype=np.int32),
            trt.PluginFieldType.INT32,
        ),
        trt.PluginField(
            "clipBoxes",
            np.array([0], dtype=np.int32),
            trt.PluginFieldType.INT32,
        ),
    ]
    plugin_field_collection = trt.PluginFieldCollection(plugin_fields)
    plugin_name = "BatchedNMSDynamic_TRT"
    plugin_version = "1"
    plugin = get_trt_plugin(
        plugin_name, plugin_field_collection, plugin_version
    )
    batch_nms_layer = network.add_plugin_v2(batch_nms_inputs, plugin)

    # dynamic shape: [bs, keep_topk, 4], [bs, keep_topk], [bs, keep_topk]
    nmsed_boxes = batch_nms_layer.get_output(1)
    nmsed_scores = batch_nms_layer.get_output(2)
    nmsed_classes = batch_nms_layer.get_output(3)
    nmsed_scores_transpose_layer = network.add_shuffle(nmsed_scores)
    nmsed_classes_reshape_layer = network.add_shuffle(nmsed_classes)
    nmsed_scores_transpose_layer.reshape_dims = trt.Dims(
        [bboxes_dims[0], keep_top_k, 1]
    )
    nmsed_classes_reshape_layer.reshape_dims = trt.Dims(
        [bboxes_dims[0], keep_top_k, 1]
    )

    concat_inputs = [
        nmsed_classes_reshape_layer.get_output(0),
        nmsed_scores_transpose_layer.get_output(0),
        nmsed_boxes,
    ]
    nms_concat_layer = network.add_concatenation(inputs=concat_inputs)
    nms_concat_layer.axis = 2
    nms_concat_output = nms_concat_layer.get_output(0)
    nms_shuffle_layer = network.add_shuffle(nms_concat_output)
    nms_shuffle_layer.reshape_dims = trt.Dims(
        [bboxes_dims[0], nms_concat_output.shape[-1]]
    )

    # add fake index as output to be consistent with the outputs of multiclass_nms3
    shape_weight = trt.Weights(np.array([0], dtype=np.int32))
    constant_layer = network.add_constant([1, 1], shape_weight)

    return (
        nms_shuffle_layer.get_output(0),
        constant_layer.get_output(0),
        batch_nms_layer.get_output(0),
    )


@converter_registry.register("pd_op.set_value", trt_version="8.x")
@converter_registry.register("pd_op.set_value_", trt_version="8.x")
@converter_registry.register("pd_op.set_value_with_tensor", trt_version="8.x")
@converter_registry.register("pd_op.set_value_with_tensor_", trt_version="8.x")
def set_value_converter(network, paddle_op, inputs):
    x = inputs[0]
    if (
        paddle_op.name() == "pd_op.set_value"
        or paddle_op.name() == "pd_op.set_value_"
    ):
        starts = (
            paddle_op.operands()[1]
            .source()
            .get_defining_op()
            .attrs()["value"][0]
        )
        ends = (
            paddle_op.operands()[2]
            .source()
            .get_defining_op()
            .attrs()["value"][0]
        )
        steps = (
            paddle_op.operands()[3]
            .source()
            .get_defining_op()
            .attrs()["value"][0]
        )
    else:
        starts = (
            paddle_op.operands()[2]
            .source()
            .get_defining_op()
            .attrs()["value"][0]
        )
        ends = (
            paddle_op.operands()[3]
            .source()
            .get_defining_op()
            .attrs()["value"][0]
        )
        steps = (
            paddle_op.operands()[4]
            .source()
            .get_defining_op()
            .attrs()["value"][0]
        )
    axes = paddle_op.attrs()["axes"][0]

    input_dims = x.shape

    # check params and refill
    if axes < 0:
        axes += len(input_dims)

    if ends < 0:
        ends += input_dims[axes]

    if ends >= input_dims[axes]:
        ends = input_dims[axes]

    if (
        paddle_op.name() == "pd_op.set_value_with_tensor"
        or paddle_op.name() == "pd_op.set_value_with_tensor_"
    ):
        updates = inputs[1]
    else:
        value = paddle_op.attrs().get("values")
        input_shape_tensor = trt_shape(network, x)
        vec_tensor = []
        for i in range(len(input_dims)):
            vec_tensor.append(
                get_shape_tensor_element(network, input_shape_tensor, i)
            )

        axes_vec = [(ends - 1 - starts) / steps + 1]
        vec_tensor[axes] = add_1D_constant_layer(network, axes_vec)
        output_shape_tensor = trt_concat(network, vec_tensor, 0)
        updates = fill_constant_layer(
            network, output_shape_tensor, len(x.shape), value, x.dtype
        )

    _logger.info(f"Set_value_op: input's dimension is {input_dims}")

    value_rank = len(updates.shape)
    input_rank = len(x.shape)

    op_name = paddle_op.name()
    assert value_rank == input_rank, (
        "value's rank is not equal to input's rank, "
        'you should modify trt_config(a TensorRTConfig object) and set trt_config.disable_ops = ["{op_name}"] to forbid this op '
    )
    _logger.info(f"Set_value_op: updates tensor's simension is {updates.shape}")

    # calculate dims
    update_dims = updates.shape
    assert (
        update_dims[axes] > 0
    ), "the update value shape[{axes}] must be greater than 0, but received {update_dims[axes]}"
    assert (
        input_dims[axes] > 0
    ), "the input shape[{axes}] must be greater than 0, but received {input_dims[axes]}"
    input_dims_rank = len(input_dims)
    assert (
        axes <= input_dims_rank
    ), "The axes {axes} is larger than total axes {input_dims_rank}"
    assert (
        starts <= input_dims[axes]
    ), "The start {starts} of dim {axes} is larger than origin shape {input_dims[axes]}"

    target_update_dim = (ends - 1 - starts) / steps + 1
    assert (
        update_dims[axes] == target_update_dim
    ), "the {axes}th axis of update dim error, should be {target_update_dim}, but we got {update_dims[axes]}"

    shape_0 = [1] * len(update_dims)
    shape_weight = trt.Weights(np.array([0], dtype=np.float32))
    zero_tensor = network.add_constant(shape_0, shape_weight).get_output(0)

    indice_tensor = trt_prod(network, zero_tensor, updates)
    cast_layer = network.add_identity(indice_tensor)
    cast_layer.set_output_type(0, trt.int32)
    indice_tensor = cast_layer.get_output(0)

    shape_1 = [1] * len(update_dims)
    shape_1[axes] = update_dims[axes]
    tmp_1 = []
    for i in range(starts, ends, steps):
        tmp_1.append(i)
    shape_weight = trt.Weights(np.array(tmp_1, dtype=np.int32))
    one_tensor = network.add_constant(shape_1, shape_weight).get_output(0)

    indice_tensor = trt_sum(network, indice_tensor, one_tensor)
    layer = network.add_scatter(
        x, indice_tensor, updates, trt.ScatterMode.ELEMENT
    )
    layer.axis = axes
    return layer.get_output(0)


@converter_registry.register("pd_op.share_data", trt_version="8.x")
def share_data_converter(network, paddle_op, inputs):
    x = inputs[0]

    identity_layer = network.add_identity(x)

    return identity_layer.get_output(0)


@converter_registry.register("pd_op.temporal_shift", trt_version="8.x")
def temporal_shift_converter(network, paddle_op, inputs):
    input_tensor = inputs[0]
    shift_ratio = paddle_op.attrs().get("shift_ratio")
    T = paddle_op.attrs().get("seg_num")
    data_format = paddle_op.attrs().get("data_format", "NCHW")

    if data_format == "NHWC":
        # Transpose input to [N, C, H, W]
        transpose_layer = network.add_shuffle(input_tensor)
        transpose_layer.first_transpose = trt.Permutation([0, 3, 1, 2])
        input_tensor = transpose_layer.get_output(0)

    input_dims = input_tensor.shape
    C, H, W = input_dims[1], input_dims[2], input_dims[3]

    # Reshape input to [N, T, C, H, W]
    reshape_layer = network.add_shuffle(input_tensor)
    reshape_layer.reshape_dims = trt.Dims([-1, T, C, H, W])
    input_tensor = reshape_layer.get_output(0)

    # Pad input to [N, T + 2, C, H, W]
    pre_pad = add_1D_constant_layer(network, [0, 1, 0, 0, 0])
    post_pad = add_1D_constant_layer(network, [0, 1, 0, 0, 0])
    dims = 5
    zeros = add_1D_constant_layer(network, [0] * dims)
    start = trt_sub(network, zeros, pre_pad)
    total_padding = trt_sum(network, pre_pad, post_pad)
    input_shape = trt_shape(network, input_tensor)
    size = trt_sum(network, input_shape, total_padding)
    stride = [1] * dims
    dummy = stride

    slice_layer = network.add_slice(input_tensor, dummy, dummy, stride)
    slice_layer.set_input(1, start)
    slice_layer.set_input(2, size)

    trt_version = trt.__version__.split('.')
    if int(trt_version[0]) > 8 or (
        int(trt_version[0]) == 8 and int(trt_version[1]) >= 5
    ):
        slice_layer.mode = trt.SampleMode.FILL
    else:
        slice_layer.mode = trt.SliceMode.FILL

    slice_c = int(C * shift_ratio)
    slice_c2 = int(C * shift_ratio * 2)

    slice_start1 = zeros
    slice_start2 = add_1D_constant_layer(network, [0, 2, slice_c, 0, 0])
    slice_start3 = add_1D_constant_layer(network, [0, 1, slice_c2, 0, 0])

    slice_size_base = trt_shape(network, input_tensor)
    sub_size1 = add_1D_constant_layer(network, [0, 0, C - slice_c, 0, 0])
    sub_size2 = add_1D_constant_layer(
        network, [0, 0, C + slice_c - slice_c2, 0, 0]
    )
    sub_size3 = add_1D_constant_layer(network, [0, 0, slice_c2, 0, 0])

    slice_size1 = trt_sub(network, slice_size_base, sub_size1)
    slice_size2 = trt_sub(network, slice_size_base, sub_size2)
    slice_size3 = trt_sub(network, slice_size_base, sub_size3)

    slice1_layer = network.add_slice(
        slice_layer.get_output(0), start=dummy, shape=dummy, stride=stride
    )
    slice1_layer.set_input(1, slice_start1)
    slice1_layer.set_input(2, slice_size1)
    slice2_layer = network.add_slice(
        slice_layer.get_output(0), start=dummy, shape=dummy, stride=stride
    )
    slice2_layer.set_input(1, slice_start2)
    slice2_layer.set_input(2, slice_size2)
    slice3_layer = network.add_slice(
        slice_layer.get_output(0), start=dummy, shape=dummy, stride=stride
    )
    slice3_layer.set_input(1, slice_start3)
    slice3_layer.set_input(2, slice_size3)

    concat_inputs = [slice2_layer.get_output(0), slice3_layer.get_output(0)]
    if slice_c == 0:
        concat_layer = network.add_concatenation(concat_inputs)
        concat_layer.axis = 2
    else:
        concat_inputs = [
            slice1_layer.get_output(0),
            slice2_layer.get_output(0),
            slice3_layer.get_output(0),
        ]
        concat_layer = network.add_concatenation(concat_inputs)
        concat_layer.axis = 2

    # Reshape output to [N*T,C,H,W]
    reshape_layer3 = network.add_shuffle(concat_layer.get_output(0))
    reshape_layer3.reshape_dims = trt.Dims(inputs[0].shape)

    if data_format == "NHWC":
        transpose_layer2 = network.add_shuffle(reshape_layer3.get_output(0))
        transpose_layer2.first_transpose = trt.Permutation([0, 2, 3, 1])
        output_tensor = transpose_layer2.get_output(0)
    else:
        output_tensor = reshape_layer3.get_output(0)

    return output_tensor
