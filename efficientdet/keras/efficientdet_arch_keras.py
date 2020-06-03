# Lint as: python3
# Copyright 2020 Google Research. All Rights Reserved.
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
# ==============================================================================
"""Keras implementation of efficientdet."""
import functools
from absl import logging
import numpy as np
import tensorflow as tf

import efficientdet_arch as legacy_arch
import hparams_config
import utils
from keras import utils_keras


class FNode(tf.keras.layers.Layer):

  def __init__(self,
               new_node_height,
               new_node_width,
               inputs_offsets,
               fpn_num_filters,
               apply_bn_for_resampling,
               is_training_bn,
               conv_after_downsample,
               use_native_resize_op,
               pooling_type,
               conv_bn_act_pattern,
               separable_conv,
               act_type,
               strategy,
               weight_method,
               data_format,
               name='fnode'):
    super(FNode, self).__init__(name=name)
    self.new_node_height = new_node_height
    self.new_node_width = new_node_width
    self.inputs_offsets = inputs_offsets
    self.fpn_num_filters = fpn_num_filters
    self.apply_bn_for_resampling = apply_bn_for_resampling
    self.separable_conv = separable_conv
    self.act_type = act_type
    self.is_training_bn = is_training_bn
    self.conv_after_downsample = conv_after_downsample
    self.use_native_resize_op = use_native_resize_op
    self.pooling_type = pooling_type
    self.strategy = strategy
    self.data_format = data_format
    self.weight_method = weight_method
    self.conv_bn_act_pattern = conv_bn_act_pattern
    self.resample_feature_maps = []
    self.op_after_combines = []
    self.vars = []

  def fuse_features(self, nodes):
    """Fuse features from different resolutions and return a weighted sum.

    Args:
      nodes: a list of tensorflow features at different levels
      weight_method: feature fusion method. One of:
        - "attn" - Softmax weighted fusion
        - "fastattn" - Fast normalzied feature fusion
        - "sum" - a sum of inputs

    Returns:
      A tensor denoting the fused feature.
    """
    dtype = nodes[0].dtype

    if self.weight_method == 'attn':
      edge_weights = []
      for _ in nodes:
        var = tf.Variable(1.0, name='WSM')
        self.vars.append(var)
        var = tf.cast(var, dtype=dtype)
        edge_weights.append(var)
      normalized_weights = tf.nn.softmax(tf.stack(edge_weights))
      nodes = tf.stack(nodes, axis=-1)
      new_node = tf.reduce_sum(nodes * normalized_weights, -1)
    elif self.weight_method == 'fastattn':
      edge_weights = []
      for _ in nodes:
        var = tf.Variable(1.0, name='WSM')
        self.vars.append(var)
        var = tf.cast(var, dtype=dtype)
        edge_weights.append(var)
      weights_sum = tf.add_n(edge_weights)
      nodes = [
          nodes[i] * edge_weights[i] / (weights_sum + 0.0001)
          for i in range(len(nodes))
      ]
      new_node = tf.add_n(nodes)
    elif self.weight_method == 'channel_attn':
      num_filters = int(nodes[0].shape[-1])
      edge_weights = []
      for _ in nodes:
        var = tf.Variable(lambda: tf.ones([num_filters]), name='WSM')
        self.vars.append(var)
        var = tf.cast(var, dtype=dtype)
        edge_weights.append(var)
      normalized_weights = tf.nn.softmax(tf.stack(edge_weights, -1), axis=-1)
      nodes = tf.stack(nodes, axis=-1)
      new_node = tf.reduce_sum(nodes * normalized_weights, -1)
    elif self.weight_method == 'channel_fastattn':
      num_filters = int(nodes[0].shape[-1])
      edge_weights = []
      for _ in nodes:
        var = tf.Variable(lambda: tf.ones([num_filters]), name='WSM')
        self.vars.append(var)
        var = tf.cast(var, dtype=dtype)
        edge_weights.append(var)

      weights_sum = tf.add_n(edge_weights)
      nodes = [
          nodes[i] * edge_weights[i] / (weights_sum + 0.0001)
          for i in range(len(nodes))
      ]
      new_node = tf.add_n(nodes)
    elif self.weight_method == 'sum':
      new_node = tf.add_n(nodes)
    else:
      raise ValueError('unknown weight_method {}'.format(self.weight_method))

    return new_node

  def call(self, feats):
    # num_output_connections = [0 for _ in feats]
    nodes = []
    for idx, input_offset in enumerate(self.inputs_offsets):
      input_node = feats[input_offset]
      # num_output_connections[input_offset] += 1
      resample_feature_map = ResampleFeatureMap(self.new_node_height,
                                                self.new_node_width,
                                                self.fpn_num_filters,
                                                self.apply_bn_for_resampling,
                                                self.is_training_bn,
                                                self.conv_after_downsample,
                                                self.use_native_resize_op,
                                                self.pooling_type,
                                                strategy=self.strategy,
                                                data_format=self.data_format,
                                                name='resample_{}_{}_{}'.format(
                                                    idx, input_offset,
                                                    len(feats)))
      self.resample_feature_maps.append(resample_feature_map)
      input_node = resample_feature_map(input_node)
      nodes.append(input_node)
    new_node = self.fuse_features(nodes)
    op_after_combine = OpAfterCombine(self.is_training_bn,
                                      self.conv_bn_act_pattern,
                                      self.separable_conv,
                                      self.fpn_num_filters,
                                      self.act_type,
                                      self.data_format,
                                      self.strategy,
                                      name='op_after_combine{}'.format(
                                          len(feats)))
    self.op_after_combines.append(op_after_combine)
    new_node = op_after_combine(new_node)
    feats.append(new_node)
    return feats
    # num_output_connections.append(0)


class OpAfterCombine(tf.keras.layers.Layer):

  def __init__(self,
               is_training_bn,
               conv_bn_act_pattern,
               separable_conv,
               fpn_num_filters,
               act_type,
               data_format,
               strategy,
               name='op_after_combine'):
    super(OpAfterCombine, self).__init__(name=name)
    self.conv_bn_act_pattern = conv_bn_act_pattern
    self.separable_conv = separable_conv
    self.fpn_num_filters = fpn_num_filters
    self.act_type = act_type
    self.data_format = data_format
    self.strategy = strategy
    self.is_training_bn = is_training_bn
    if self.separable_conv:
      Conv2D = functools.partial(tf.keras.layers.SeparableConv2D,
                                 depth_multiplier=1)
    else:
      Conv2D = tf.keras.layers.Conv2D

    self.conv_op = Conv2D(filters=fpn_num_filters,
                          kernel_size=(3, 3),
                          padding='same',
                          use_bias=not self.conv_bn_act_pattern,
                          data_format=self.data_format,
                          name='conv')
    self.bn = utils_keras.build_batch_norm(
        is_training_bn=self.is_training_bn,
        data_format=self.data_format,
        strategy=self.strategy,
        name='bn'
    )

  def call(self, new_node):
    if not self.conv_bn_act_pattern:
      new_node = utils.activation_fn(new_node, self.act_type)
    new_node = self.conv_op(new_node)
    new_node = self.bn(new_node)
    act_type = None if not self.conv_bn_act_pattern else self.act_type
    if act_type:
      new_node = utils.activation_fn(new_node, act_type)
    return new_node


def build_bifpn_layer(feats, feat_sizes, config):
  """Builds a feature pyramid given previous feature pyramid and config."""
  p = config  # use p to denote the network config.
  if p.fpn_config:
    fpn_config = p.fpn_config
  else:
    fpn_config = legacy_arch.get_fpn_config(p.fpn_name, p.min_level,
                                            p.max_level, p.fpn_weight_method)

  for i, fnode in enumerate(fpn_config.nodes):
    logging.info('fnode %d : %s', i, fnode)
    feats = FNode(feat_sizes[fnode['feat_level']]['height'],
                  feat_sizes[fnode['feat_level']]['width'],
                  fnode['inputs_offsets'],
                  p.fpn_num_filters,
                  p.apply_bn_for_resampling,
                  p.is_training_bn,
                  p.conv_after_downsample,
                  p.use_native_resize_op,
                  p.pooling_type,
                  p.conv_bn_act_pattern,
                  p.separable_conv,
                  p.act_type,
                  strategy=p.strategy,
                  weight_method=fpn_config.weight_method,
                  data_format=config.data_format,
                  name='fnode{}'.format(i))(feats)

  output_feats = {}
  for l in range(p.min_level, p.max_level + 1):
    for i, fnode in enumerate(reversed(fpn_config.nodes)):
      if fnode['feat_level'] == l:
        output_feats[l] = feats[-1 - i]
        break
  return output_feats


class ResampleFeatureMap(tf.keras.layers.Layer):
  """Resample feature map for downsampling or upsampling."""

  def __init__(self,
               target_height,
               target_width,
               target_num_channels,
               apply_bn=False,
               is_training=None,
               conv_after_downsample=False,
               use_native_resize_op=False,
               pooling_type=None,
               strategy=None,
               data_format=None,
               name='resample_p0'):
    super(ResampleFeatureMap, self).__init__(name=name)
    self.apply_bn = apply_bn
    self.is_training = is_training
    self.data_format = data_format
    self.target_num_channels = target_num_channels
    self.target_height = target_height
    self.target_width = target_width
    self.strategy = strategy
    self.conv_after_downsample = conv_after_downsample
    self.use_native_resize_op = use_native_resize_op
    self.pooling_type = pooling_type
    self.conv2d = tf.keras.layers.Conv2D(self.target_num_channels, (1, 1),
                                         padding='same',
                                         data_format=self.data_format,
                                         name='conv2d')
    self.bn = utils_keras.build_batch_norm(is_training_bn=self.is_training,
                                           data_format=self.data_format,
                                           strategy=self.strategy,
                                           name='bn')

  def build(self, input_shape):
    """Resample input feature map to have target number of channels and size."""
    if self.data_format == 'channels_first':
      _, num_channels, height, width = input_shape.as_list()
    else:
      _, height, width, num_channels = input_shape.as_list()

    if height is None or width is None or num_channels is None:
      raise ValueError(
          'shape[1] or shape[2] or shape[3] of feat is None (shape:{}).'.format(
              input_shape.as_list()))
    if self.apply_bn and self.is_training is None:
      raise ValueError('If BN is applied, need to provide is_training')
    self.num_channels = num_channels
    self.height = height
    self.width = width
    height_stride_size = int((self.height - 1) // self.target_height + 1)
    width_stride_size = int((self.width - 1) // self.target_width + 1)

    if self.pooling_type == 'max' or self.pooling_type is None:
      # Use max pooling in default.
      self.pool2d = tf.keras.layers.MaxPooling2D(
          pool_size=[height_stride_size + 1, width_stride_size + 1],
          strides=[height_stride_size, width_stride_size],
          padding='SAME',
          data_format=self.data_format)
    elif self.pooling_type == 'avg':
      self.pool2d = tf.keras.layers.AveragePooling2D(
          pool_size=[height_stride_size + 1, width_stride_size + 1],
          strides=[height_stride_size, width_stride_size],
          padding='SAME',
          data_format=self.data_format)
    else:
      raise ValueError('Unknown pooling type: {}'.format(self.pooling_type))

    height_scale = self.target_height // self.height
    width_scale = self.target_width // self.width
    if (self.use_native_resize_op or self.target_height % self.height != 0 or
        self.target_width % self.width != 0):
      self.upsample2d = tf.keras.layers.UpSampling2D(
          (height_scale, width_scale), data_format=self.data_format)
    else:
      self.upsample2d = functools.partial(legacy_arch.nearest_upsampling,
                                          height_scale=height_scale,
                                          width_scale=width_scale,
                                          data_format=self.data_format)
    super(ResampleFeatureMap, self).build(input_shape)

  def _maybe_apply_1x1(self, feat):
    """Apply 1x1 conv to change layer width if necessary."""
    if self.num_channels != self.target_num_channels:
      feat = self.conv2d(feat)
      if self.apply_bn:
        feat = self.bn(feat, training=self.is_training)
    return feat

  def call(self, feat):
    # If conv_after_downsample is True, when downsampling, apply 1x1 after
    # downsampling for efficiency.
    if self.height > self.target_height and self.width > self.target_width:
      if not self.conv_after_downsample:
        feat = self._maybe_apply_1x1(feat)
      feat = self.pool2d(feat)
      if self.conv_after_downsample:
        feat = self._maybe_apply_1x1(feat)
    elif self.height <= self.target_height and self.width <= self.target_width:
      feat = self._maybe_apply_1x1(feat)
      if self.height < self.target_height or self.width < self.target_width:
        feat = self.upsample2d(feat)
    else:
      raise ValueError(
          'Incompatible target feature map size: target_height: {},'
          'target_width: {}'.format(self.target_height, self.target_width))

    return feat

  def get_config(self):
    config = {
        'apply_bn': self.apply_bn,
        'is_training': self.is_training,
        'data_format': self.data_format,
        'target_num_channels': self.target_num_channels,
        'target_height': self.target_height,
        'target_width': self.target_width,
        'strategy': self.strategy,
        'conv_after_downsample': self.conv_after_downsample,
        'use_native_resize_op': self.use_native_resize_op,
        'pooling_type': self.pooling_type,
    }
    base_config = super(ResampleFeatureMap, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))


class ClassNet(tf.keras.layers.Layer):
  """Object class prediction network."""

  def __init__(self,
               num_classes=90,
               num_anchors=9,
               num_filters=32,
               min_level=3,
               max_level=7,
               is_training=False,
               act_type='swish',
               repeats=4,
               separable_conv=True,
               survival_prob=None,
               strategy=None,
               data_format='channels_last',
               name='class_net',
               **kwargs):
    """Initialize the ClassNet.

    Args:
      num_classes: number of classes.
      num_anchors: number of anchors.
      num_filters: number of filters for "intermediate" layers.
      min_level: minimum level for features.
      max_level: maximum level for features.
      is_training: True if we train the BatchNorm.
      act_type: String of the activation used.
      repeats: number of intermediate layers.
      separable_conv: True to use separable_conv instead of conv2D.
      survival_prob: if a value is set then drop connect will be used.
      strategy: string to specify training strategy for TPU/GPU/CPU.
      data_format: string of 'channel_first' or 'channels_last'.
      name: the name of this layerl.
      **kwargs: other parameters.
    """

    super(ClassNet, self).__init__(name=name, **kwargs)
    self.num_classes = num_classes
    self.num_anchors = num_anchors
    self.num_filters = num_filters
    self.min_level = min_level
    self.max_level = max_level
    self.repeats = repeats
    self.separable_conv = separable_conv
    self.is_training = is_training
    self.survival_prob = survival_prob
    self.act_type = act_type
    self.strategy = strategy
    self.data_format = data_format
    self.use_dc = survival_prob and is_training

    self.conv_ops = []
    self.bns = []

    for i in range(self.repeats):
      # If using SeparableConv2D
      if self.separable_conv:
        self.conv_ops.append(
            tf.keras.layers.SeparableConv2D(
                filters=self.num_filters,
                depth_multiplier=1,
                pointwise_initializer=tf.initializers.VarianceScaling(),
                depthwise_initializer=tf.initializers.VarianceScaling(),
                data_format=self.data_format,
                kernel_size=3,
                activation=None,
                bias_initializer=tf.zeros_initializer(),
                padding='same',
                name='class-%d' % i))
      # If using Conv2d
      else:
        self.conv_ops.append(
            tf.keras.layers.Conv2D(
                filters=self.num_filters,
                kernel_initializer=tf.random_normal_initializer(stddev=0.01),
                data_format=self.data_format,
                kernel_size=3,
                activation=None,
                bias_initializer=tf.zeros_initializer(),
                padding='same',
                name='class-%d' % i))

      bn_per_level = {}
      for level in range(self.min_level, self.max_level + 1):
        bn_per_level[level] = utils_keras.build_batch_norm(
            is_training_bn=self.is_training,
            init_zero=False,
            strategy=self.strategy,
            data_format=self.data_format,
            name='class-%d-bn-%d' % (i, level),
        )
      self.bns.append(bn_per_level)

    if self.separable_conv:
      self.classes = tf.keras.layers.SeparableConv2D(
          filters=self.num_classes * self.num_anchors,
          depth_multiplier=1,
          pointwise_initializer=tf.initializers.VarianceScaling(),
          depthwise_initializer=tf.initializers.VarianceScaling(),
          data_format=self.data_format,
          kernel_size=3,
          activation=None,
          bias_initializer=tf.constant_initializer(-np.math.log((1 - 0.01) /
                                                                0.01)),
          padding='same',
          name='class-predict')

    else:
      self.classes = tf.keras.layers.Conv2D(
          filters=self.num_classes * self.num_anchors,
          kernel_initializer=tf.random_normal_initializer(stddev=0.01),
          data_format=self.data_format,
          kernel_size=3,
          activation=None,
          bias_initializer=tf.constant_initializer(-np.math.log((1 - 0.01) /
                                                                0.01)),
          padding='same',
          name='class-predict')

  def call(self, inputs, **kwargs):
    """Call ClassNet."""

    class_outputs = {}
    for level in range(self.min_level, self.max_level + 1):
      image = inputs[level]
      for i in range(self.repeats):
        original_image = image
        image = self.conv_ops[i](image)
        image = self.bns[i][level](image, training=self.is_training)
        if self.act_type:
          image = utils.activation_fn(image, self.act_type)
        if i > 0 and self.use_dc:
          image = utils.drop_connect(image, self.is_training,
                                     self.survival_prob)
          image = image + original_image

      class_outputs[level] = self.classes(image)

    return class_outputs

  def get_config(self):
    base_config = super(ClassNet, self).get_config()

    return {
        **base_config,
        'num_classes': self.num_classes,
        'num_anchors': self.num_anchors,
        'num_filters': self.num_filters,
        'min_level': self.min_level,
        'max_level': self.max_level,
        'is_training': self.is_training,
        'act_type': self.act_type,
        'repeats': self.repeats,
        'separable_conv': self.separable_conv,
        'survival_prob': self.survival_prob,
        'strategy': self.strategy,
        'data_format': self.data_format,
    }


class BoxNet(tf.keras.layers.Layer):
  """Box regression network."""

  def __init__(self,
               num_anchors=9,
               num_filters=32,
               min_level=3,
               max_level=7,
               is_training=False,
               act_type='swish',
               repeats=4,
               separable_conv=True,
               survival_prob=None,
               strategy=None,
               data_format='channels_last',
               name='box_net',
               **kwargs):
    """Initialize BoxNet.

    Args:
      num_anchors: number of  anchors used.
      num_filters: number of filters for "intermediate" layers.
      min_level: minimum level for features.
      max_level: maximum level for features.
      is_training: True if we train the BatchNorm.
      act_type: String of the activation used.
      repeats: number of "intermediate" layers.
      separable_conv: True to use separable_conv instead of conv2D.
      survival_prob: if a value is set then drop connect will be used.
      strategy: string to specify training strategy for TPU/GPU/CPU.
      data_format: string of 'channel_first' or 'channels_last'.
      name: Name of the layer.
      **kwargs: other parameters.
    """

    super(BoxNet, self).__init__(name=name, **kwargs)

    self.num_anchors = num_anchors
    self.num_filters = num_filters
    self.min_level = min_level
    self.max_level = max_level
    self.repeats = repeats
    self.separable_conv = separable_conv
    self.is_training = is_training
    self.survival_prob = survival_prob
    self.act_type = act_type
    self.strategy = strategy
    self.data_format = data_format
    self.use_dc = survival_prob and is_training

    self.conv_ops = []
    self.bns = []

    for i in range(self.repeats):
      # If using SeparableConv2D
      if self.separable_conv:
        self.conv_ops.append(
            tf.keras.layers.SeparableConv2D(
                filters=self.num_filters,
                depth_multiplier=1,
                pointwise_initializer=tf.initializers.VarianceScaling(),
                depthwise_initializer=tf.initializers.VarianceScaling(),
                data_format=self.data_format,
                kernel_size=3,
                activation=None,
                bias_initializer=tf.zeros_initializer(),
                padding='same',
                name='box-%d' % i))
      # If using Conv2d
      else:
        self.conv_ops.append(
            tf.keras.layers.Conv2D(
                filters=self.num_filters,
                kernel_initializer=tf.random_normal_initializer(stddev=0.01),
                data_format=self.data_format,
                kernel_size=3,
                activation=None,
                bias_initializer=tf.zeros_initializer(),
                padding='same',
                name='box-%d' % i))

      bn_per_level = {}
      for level in range(self.min_level, self.max_level + 1):
        bn_per_level[level] = utils_keras.build_batch_norm(
            is_training_bn=self.is_training,
            init_zero=False,
            strategy=self.strategy,
            data_format=self.data_format,
            name='box-%d-bn-%d' % (i, level))
      self.bns.append(bn_per_level)

    if self.separable_conv:
      self.boxes = tf.keras.layers.SeparableConv2D(
          filters=4 * self.num_anchors,
          depth_multiplier=1,
          pointwise_initializer=tf.initializers.VarianceScaling(),
          depthwise_initializer=tf.initializers.VarianceScaling(),
          data_format=self.data_format,
          kernel_size=3,
          activation=None,
          bias_initializer=tf.zeros_initializer(),
          padding='same',
          name='box-predict')

    else:
      self.boxes = tf.keras.layers.Conv2D(
          filters=4 * self.num_anchors,
          kernel_initializer=tf.random_normal_initializer(stddev=0.01),
          data_format=self.data_format,
          kernel_size=3,
          activation=None,
          bias_initializer=tf.zeros_initializer(),
          padding='same',
          name='box-predict')

  def call(self, inputs, **kwargs):
    """Call boxnet."""
    box_outputs = {}
    for level in range(self.min_level, self.max_level + 1):
      image = inputs[level]
      for i in range(self.repeats):
        original_image = image
        image = self.conv_ops[i](image)
        image = self.bns[i][level](image, training=self.is_training)
        if self.act_type:
          image = utils.activation_fn(image, self.act_type)
        if i > 0 and self.use_dc:
          image = utils.drop_connect(image, self.is_training,
                                     self.survival_prob)
          image = image + original_image

      box_outputs[level] = self.boxes(image)

    return box_outputs

  def get_config(self):
    base_config = super(BoxNet, self).get_config()

    return {
        **base_config,
        'num_anchors': self.num_anchors,
        'num_filters': self.num_filters,
        'min_level': self.min_level,
        'max_level': self.max_level,
        'is_training': self.is_training,
        'act_type': self.act_type,
        'repeats': self.repeats,
        'separable_conv': self.separable_conv,
        'survival_prob': self.survival_prob,
        'strategy': self.strategy,
        'data_format': self.data_format,
    }


def build_feature_network(features, config):
  """Build FPN input features.

  Args:
   features: input tensor.
   config: a dict-like config, including all parameters.

  Returns:
    A dict from levels to the feature maps processed after feature network.
  """
  feat_sizes = utils.get_feat_sizes(config.image_size, config.max_level)
  feats = []
  if config.min_level not in features.keys():
    raise ValueError('features.keys ({}) should include min_level ({})'.format(
        features.keys(), config.min_level))

  # Build additional input features that are not from backbone.
  for level in range(config.min_level, config.max_level + 1):
    if level in features.keys():
      feats.append(features[level])
    else:
      h_id, w_id = (2, 3) if config.data_format == 'channels_first' else (1, 2)
      # Adds a coarser level by downsampling the last feature map.
      feats.append(
          ResampleFeatureMap(
              target_height=(feats[-1].shape[h_id] - 1) // 2 + 1,
              target_width=(feats[-1].shape[w_id] - 1) // 2 + 1,
              target_num_channels=config.fpn_num_filters,
              apply_bn=config.apply_bn_for_resampling,
              is_training=config.is_training_bn,
              conv_after_downsample=config.conv_after_downsample,
              use_native_resize_op=config.use_native_resize_op,
              pooling_type=config.pooling_type,
              strategy=config.strategy,
              data_format=config.data_format,
              name='resample_p{}'.format(level),
          )(feats[-1]))

  utils.verify_feats_size(feats,
                          feat_sizes=feat_sizes,
                          min_level=config.min_level,
                          max_level=config.max_level,
                          data_format=config.data_format)

  with tf.name_scope('fpn_cells'):
    for rep in range(config.fpn_cell_repeats):
      with tf.name_scope('cell_{}'.format(rep)):
        logging.info('building cell %d', rep)
        new_feats = build_bifpn_layer(feats, feat_sizes, config)

        feats = [
            new_feats[level]
            for level in range(config.min_level, config.max_level + 1)
        ]

        utils.verify_feats_size(feats,
                                feat_sizes=feat_sizes,
                                min_level=config.min_level,
                                max_level=config.max_level,
                                data_format=config.data_format)

  return new_feats


def build_class_and_box_outputs(feats, config):
  """Builds box net and class net.

  Args:
   feats: input tensor.
   config: a dict-like config, including all parameters.

  Returns:
   A tuple (class_outputs, box_outputs) for class/box predictions.
  """
  num_anchors = len(config.aspect_ratios) * config.num_scales
  num_filters = config.fpn_num_filters
  class_outputs = ClassNet(num_classes=config.num_classes,
                           num_anchors=num_anchors,
                           num_filters=num_filters,
                           min_level=config.min_level,
                           max_level=config.max_level,
                           is_training=config.is_training_bn,
                           act_type=config.act_type,
                           repeats=config.box_class_repeats,
                           separable_conv=config.separable_conv,
                           survival_prob=config.survival_prob,
                           strategy=config.strategy,
                           data_format=config.data_format)(feats)

  box_outputs = BoxNet(num_anchors=num_anchors,
                       num_filters=num_filters,
                       min_level=config.min_level,
                       max_level=config.max_level,
                       is_training=config.is_training_bn,
                       act_type=config.act_type,
                       repeats=config.box_class_repeats,
                       separable_conv=config.separable_conv,
                       survival_prob=config.survival_prob,
                       strategy=config.strategy,
                       data_format=config.data_format)(feats)

  return class_outputs, box_outputs


def efficientdet(model_name=None, config=None, **kwargs):
  """Build EfficientDet model.

  Args:
    features: input tensor.
    model_name: String of the model (eg. efficientdet-d0)
    config: Dict of parameters for the network
    **kwargs: other parameters.

  Returns:
    A tuple (class_outputs, box_outputs) for predictions.
  """
  if not config and not model_name:
    raise ValueError('please specify either model name or config')

  if not config:
    config = hparams_config.get_efficientdet_config(model_name)
  elif isinstance(config, dict):
    config = hparams_config.Config(config)  # wrap dict in Config object

  if kwargs:
    config.override(kwargs)

  logging.info(config)
  features = tf.keras.layers.Input([*utils.parse_image_size(config.image_size), 3])
  # build backbone features.
  features = legacy_arch.build_backbone(features, config)
  logging.info('backbone params/flops = {:.6f}M, {:.9f}B'.format(
      *utils.num_params_flops()))

  # build feature network.
  fpn_feats = build_feature_network(features, config)
  logging.info('backbone+fpn params/flops = {:.6f}M, {:.9f}B'.format(
      *utils.num_params_flops()))

  # build class and box predictions.
  class_outputs, box_outputs = build_class_and_box_outputs(fpn_feats, config)
  logging.info('backbone+fpn+box params/flops = {:.6f}M, {:.9f}B'.format(
      *utils.num_params_flops()))

  return tf.keras.Model(inputs=features, outputs=[class_outputs, box_outputs])
