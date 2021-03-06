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

from absl import logging
from keras.efficientdet_keras import EfficientDetNet
import re
import math
import iou_utils
import tensorflow as tf


def update_learning_rate_schedule_parameters(params):
  """Updates params that are related to the learning rate schedule."""
  # params['batch_size'] is per-shard within model_fn if strategy=tpu.
  batch_size = params['batch_size'] * params['num_shards']
  # Learning rate is proportional to the batch size
  params['adjusted_learning_rate'] = (params['learning_rate'] * batch_size / 64)
  steps_per_epoch = params['num_examples_per_epoch'] / batch_size
  params['lr_warmup_step'] = int(params['lr_warmup_epoch'] * steps_per_epoch)
  params['first_lr_drop_step'] = int(params['first_lr_drop_epoch'] *
                                     steps_per_epoch)
  params['second_lr_drop_step'] = int(params['second_lr_drop_epoch'] *
                                      steps_per_epoch)
  params['total_steps'] = int(params['num_epochs'] * steps_per_epoch)


class StepwiseLrSchedule(tf.optimizers.schedules.LearningRateSchedule):

  def __init__(self, adjusted_lr: float, lr_warmup_init: float,
               lr_warmup_step: int, first_lr_drop_step: int,
               second_lr_drop_step: int):
    """Build a StepwiseLrSchedule.

    Args:
      adjusted_lr: `float`, The initial learning rate.
      lr_warmup_init: `float`, The warm up learning rate.
      lr_warmup_step: `int`, The warm up step.
      first_lr_drop_step: `int`, First lr decay step.
      second_lr_drop_step: `int`, Second lr decay step.
    """
    super().__init__()
    logging.info('LR schedule method: stepwise')
    self.adjusted_lr = adjusted_lr
    self.lr_warmup_init = lr_warmup_init
    self.lr_warmup_step = lr_warmup_step
    self.first_lr_drop_step = first_lr_drop_step
    self.second_lr_drop_step = second_lr_drop_step

  def __call__(self, step):
    linear_warmup = (
        self.lr_warmup_init +
        (tf.cast(step, dtype=tf.float32) / self.lr_warmup_step *
         (self.adjusted_lr - self.lr_warmup_init)))
    learning_rate = tf.where(step < self.lr_warmup_step, linear_warmup,
                             self.adjusted_lr)
    lr_schedule = [[1.0, self.lr_warmup_step], [0.1, self.first_lr_drop_step],
                   [0.01, self.second_lr_drop_step]]
    for mult, start_global_step in lr_schedule:
      learning_rate = tf.where(step < start_global_step, learning_rate,
                               self.adjusted_lr * mult)
    return learning_rate


class CosineLrSchedule(tf.optimizers.schedules.LearningRateSchedule):

  def __init__(self, adjusted_lr: float, lr_warmup_init: float,
               lr_warmup_step: int, total_steps: int):
    """Build a CosineLrSchedule.

    Args:
      adjusted_lr: `float`, The initial learning rate.
      lr_warmup_init: `float`, The warm up learning rate.
      lr_warmup_step: `int`, The warm up step.
      total_steps: `int`, Total train steps.
    """
    super().__init__()
    logging.info('LR schedule method: cosine')
    self.adjusted_lr = adjusted_lr
    self.lr_warmup_init = lr_warmup_init
    self.lr_warmup_step = lr_warmup_step
    self.total_steps = total_steps

  def __call__(self, step):

    linear_warmup = (
        self.lr_warmup_init +
        (tf.cast(step, dtype=tf.float32) / self.lr_warmup_step *
         (self.adjusted_lr - self.lr_warmup_init)))
    cosine_lr = 0.5 * self.adjusted_lr * (
        1 + tf.cos(math.pi * tf.cast(step, tf.float32) / self.total_steps))
    return tf.where(step < self.lr_warmup_step, linear_warmup, cosine_lr)


class PolynomialLrSchedule(tf.optimizers.schedules.LearningRateSchedule):

  def __init__(self, adjusted_lr: float, lr_warmup_init: float,
               lr_warmup_step: int, power: float, total_steps: int):
    """Build a PolynomialLrSchedule.

    Args:
      adjusted_lr: `float`, The initial learning rate.
      lr_warmup_init: `float`, The warm up learning rate.
      lr_warmup_step: `int`, The warm up step.
      power: `float`, power.
      total_steps: `int`, Total train steps.
    """
    super().__init__()
    logging.info('LR schedule method: polynomial')
    self.adjusted_lr = adjusted_lr
    self.lr_warmup_init = lr_warmup_init
    self.lr_warmup_step = lr_warmup_step
    self.power = power
    self.total_steps = total_steps

  def __call__(self, step):
    linear_warmup = (
        self.lr_warmup_init +
        (tf.cast(step, dtype=tf.float32) / self.lr_warmup_step *
         (self.adjusted_lr - self.lr_warmup_init)))
    polynomial_lr = self.adjusted_lr * tf.pow(
        1 - (tf.cast(step, dtype=tf.float32) / self.total_steps), self.power)
    return tf.where(step < self.lr_warmup_step, linear_warmup, polynomial_lr)


def learning_rate_schedule(params: dict):
  """Learning rate schedule based on global step."""
  update_learning_rate_schedule_parameters(params)
  lr_decay_method = params['lr_decay_method']
  if lr_decay_method == 'stepwise':
    return StepwiseLrSchedule(params['adjusted_learning_rate'],
                              params['lr_warmup_init'],
                              params['lr_warmup_step'],
                              params['first_lr_drop_step'],
                              params['second_lr_drop_step'])

  if lr_decay_method == 'cosine':
    return CosineLrSchedule(params['adjusted_learning_rate'],
                            params['lr_warmup_init'], params['lr_warmup_step'],
                            params['total_steps'])

  if lr_decay_method == 'polynomial':
    return PolynomialLrSchedule(params['adjusted_learning_rate'],
                                params['lr_warmup_init'],
                                params['lr_warmup_step'],
                                params['poly_lr_power'], params['total_steps'])

  raise ValueError('unknown lr_decay_method: {}'.format(lr_decay_method))


def get_optimizer(params: dict):
  learning_rate = learning_rate_schedule(params)
  if params['optimizer'].lower() == 'sgd':
    logging.info("Use SGD optimizer")
    optimizer = tf.keras.optimizers.SGD(
        learning_rate, momentum=params['momentum'])
  elif params['optimizer'].lower() == 'adam':
    logging.info("Use Adam optimizer")
    optimizer = tf.keras.optimizers.Adam(learning_rate)
  else:
    raise ValueError('optimizers should be adam or sgd')
  if params['mixed_precision']:
    optimizer = tf.keras.mixed_precision.experimental.LossScaleOptimizer(
        optimizer, loss_scale='dynamic')
  return optimizer


def get_callbacks(params: dict, profile=False):
  tb_callback = tf.keras.callbacks.TensorBoard(
      log_dir=params['model_dir'], profile_batch=2 if profile else 0)
  ckpt_callback = tf.keras.callbacks.ModelCheckpoint(
      params['model_dir'], verbose=1, save_weights_only=True)
  early_stopping = tf.keras.callbacks.EarlyStopping(
      monitor='val_loss', min_delta=0, patience=10, verbose=1)
  return [tb_callback, ckpt_callback, early_stopping]


class FocalLoss(tf.keras.losses.Loss):
  """Compute the focal loss between `logits` and the golden `target` values.

  Focal loss = -(1-pt)^gamma * log(pt)
  where pt is the probability of being classified to the true class.

  Args:
    y_pred: A float32 tensor of size [batch, height_in, width_in,
      num_predictions].
    y_true: A float32 tensor of size [batch, height_in, width_in,
      num_predictions].
    alpha: A float32 scalar multiplying alpha to the loss from positive examples
      and (1-alpha) to the loss from negative examples.
    gamma: A float32 scalar modulating loss from hard and easy examples.
    normalizer: Divide loss by this value.
    label_smoothing: Float in [0, 1]. If > `0` then smooth the labels.

  Returns:
    loss: A float32 scalar representing normalized total loss.
  """

  def __init__(self, alpha, gamma, label_smoothing=0.0, **kwargs):
    super().__init__(**kwargs)
    self.alpha = alpha
    self.gamma = gamma
    self.label_smoothing = label_smoothing

  @tf.autograph.experimental.do_not_convert
  def call(self, y, y_pred):
    normalizer, y_true = y
    alpha = tf.convert_to_tensor(self.alpha, dtype=y_pred.dtype)
    gamma = tf.convert_to_tensor(self.gamma, dtype=y_pred.dtype)

    # compute focal loss multipliers before label smoothing, such that it will
    # not blow up the loss.
    pred_prob = tf.sigmoid(y_pred)
    p_t = (y_true * pred_prob) + ((1 - y_true) * (1 - pred_prob))
    alpha_factor = y_true * alpha + (1 - y_true) * (1 - alpha)
    modulating_factor = (1.0 - p_t)**gamma

    # apply label smoothing for cross_entropy for each entry.
    y_true = y_true * (1.0 - self.label_smoothing) + 0.5 * self.label_smoothing
    ce = tf.nn.sigmoid_cross_entropy_with_logits(labels=y_true, logits=y_pred)

    # compute the final loss and return
    return alpha_factor * modulating_factor * ce / normalizer


class BoxLoss(tf.keras.losses.Loss):
  """Computes box regression loss.

  delta is typically around the mean value of regression target.
  for instances, the regression targets of 512x512 input with 6 anchors on
  P3-P7 pyramid is about [0.1, 0.1, 0.2, 0.2].

  Args:
    delta: `float`, the point where the huber loss function changes from a
      quadratic to linear.
  """

  def __init__(self, delta=0.1, **kwargs):
    super().__init__(**kwargs)
    self.huber = tf.keras.losses.Huber(
        delta, reduction=tf.keras.losses.Reduction.NONE)

  @tf.autograph.experimental.do_not_convert
  def call(self, y_true, box_outputs):
    num_positives, box_targets = y_true
    normalizer = num_positives * 4.0
    mask = tf.cast(box_targets != 0.0, tf.float32)
    box_targets = tf.expand_dims(box_targets, axis=-1)
    box_outputs = tf.expand_dims(box_outputs, axis=-1)
    box_loss = self.huber(box_targets, box_outputs) * mask
    box_loss = tf.reduce_sum(box_loss)
    box_loss /= normalizer
    return box_loss


class BoxIouLoss(tf.keras.losses.Loss):

  def __init__(self, iou_loss_type, **kwargs):
    super().__init__(**kwargs)
    self.iou_loss_type = iou_loss_type

  @tf.autograph.experimental.do_not_convert
  def call(self, y_true, box_outputs):
    num_positives, box_targets = y_true
    normalizer = num_positives * 4.0
    box_iou_loss = iou_utils.iou_loss(box_outputs, box_targets,
                                      self.iou_loss_type)
    box_iou_loss = tf.reduce_sum(box_iou_loss) / normalizer
    return box_iou_loss


class EfficientDetNetTrain(EfficientDetNet):

  def __init__(self, *args, **kwargs):
    super(EfficientDetNetTrain, self).__init__(*args, **kwargs)
    self.step = tf.Variable(
        0,
        trainable=False,
        name='global_step',
        dtype=tf.int64,
        aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
        shape=[])
    if self.config.moving_average_decay:
      self.ema = tf.train.ExponentialMovingAverage(
          decay=self.config.moving_average_decay, num_updates=self.step)

  def freeze_vars(self, pattern):
    """Freeze variables according to pattern.

    Args:
      pattern: a reg experession such as ".*(efficientnet|fpn_cells).*".
    """
    if pattern:
      for v in self.trainable_variables:
        if re.match(pattern, v.name):
          v.trainable = False

  def _reg_l2_loss(self, weight_decay, regex=r'.*(kernel|weight):0$'):
    """Return regularization l2 loss loss."""
    var_match = re.compile(regex)
    return weight_decay * tf.add_n([
        tf.nn.l2_loss(v)
        for v in self.trainable_variables
        if var_match.match(v.name)
    ])

  def _detection_loss(self, cls_outputs, box_outputs, labels):
    """Computes total detection loss.

    Computes total detection loss including box and class loss from all levels.
    Args:
      cls_outputs: an OrderDict with keys representing levels and values
        representing logits in [batch_size, height, width, num_anchors].
      box_outputs: an OrderDict with keys representing levels and values
        representing box regression targets in [batch_size, height, width,
        num_anchors * 4].
      labels: the dictionary that returned from dataloader that includes
        groundtruth targets.

    Returns:
      total_loss: an integer tensor representing total loss reducing from
        class and box losses from all levels.
      cls_loss: an integer tensor representing total class loss.
      box_loss: an integer tensor representing total box regression loss.
      box_iou_loss: an integer tensor representing total box iou loss.
    """
    # Sum all positives in a batch for normalization and avoid zero
    # num_positives_sum, which would lead to inf loss during training
    num_positives_sum = tf.reduce_sum(labels['mean_num_positives']) + 1.0
    levels = range(len(cls_outputs))
    cls_losses = []
    box_losses = []
    box_iou_losses = []
    for level in levels:
      # Onehot encoding for classification labels.
      cls_targets_at_level = tf.one_hot(labels['cls_targets_%d' % (level + 3)],
                                        self.config.num_classes)

      if self.config.data_format == 'channels_first':
        bs, _, width, height, _ = cls_targets_at_level.get_shape().as_list()
        cls_targets_at_level = tf.reshape(cls_targets_at_level,
                                          [bs, -1, width, height])
      else:
        bs, width, height, _, _ = cls_targets_at_level.get_shape().as_list()
        cls_targets_at_level = tf.reshape(cls_targets_at_level,
                                          [bs, width, height, -1])
      box_targets_at_level = labels['box_targets_%d' % (level + 3)]

      class_loss = self.loss.get("class_loss", None)
      cls_loss = class_loss([num_positives_sum, cls_outputs[level]],
                            cls_targets_at_level)

      if self.config.data_format == 'channels_first':
        cls_loss = tf.reshape(cls_loss,
                              [bs, -1, width, height, self.config.num_classes])
      else:
        cls_loss = tf.reshape(cls_loss,
                              [bs, width, height, -1, self.config.num_classes])
      cls_loss *= tf.cast(
          tf.expand_dims(
              tf.not_equal(labels['cls_targets_%d' % (level + 3)], -2), -1),
          tf.float32)
      cls_losses.append(tf.reduce_sum(cls_loss))

      if self.config.box_loss_weight:
        box_loss = self.loss.get("box_loss", None)
        box_losses.append(
            box_loss([num_positives_sum, box_outputs[level]],
                     box_targets_at_level))

      if self.config.iou_loss_type:
        box_iou_loss = self.loss.get("box_iou_loss", None)
        box_iou_losses.append(
            box_iou_loss(box_outputs[level], box_targets_at_level,
                         num_positives_sum, self.config.iou_loss_type))
    cls_loss = tf.add_n(cls_losses)
    box_loss = tf.add_n(box_losses) if box_losses else 0
    box_iou_loss = tf.add_n(box_iou_losses) if box_iou_losses else 0
    total_loss = (
        cls_loss + self.config.box_loss_weight * box_loss +
        self.config.iou_loss_weight * box_iou_loss)
    return total_loss, cls_loss, box_loss, box_iou_loss

  def train_step(self, data):
    """Train step.

    Args:
      data: Tuple of (images, labels). Image tensor with shape [batch_size, height, width, 3].
        The height and width are fixed and equal.Input labels in a dictionary. The labels include class targets
        and box targets which are dense label maps. The labels are generated from
        get_input_fn function in data/dataloader.py.

    Returns:
      A dict record loss info.
    """
    images, labels = data
    with tf.GradientTape() as tape:
      cls_outputs, box_outputs = self(images, training=True)
      det_loss, cls_loss, box_loss, box_iou_loss = self._detection_loss(
          cls_outputs, box_outputs, labels)
      reg_l2loss = self._reg_l2_loss(self.config.weight_decay)
      total_loss = det_loss + reg_l2loss
      if isinstance(self.optimizer,
                    tf.keras.mixed_precision.experimental.LossScaleOptimizer):
        scaled_loss = self.optimizer.get_scaled_loss(total_loss)
      else:
        scaled_loss = total_loss
    trainable_vars = self.trainable_variables
    scaled_gradients = tape.gradient(scaled_loss, trainable_vars)
    if isinstance(self.optimizer,
                  tf.keras.mixed_precision.experimental.LossScaleOptimizer):
      gradients = self.optimizer.get_unscaled_gradients(scaled_gradients)
    else:
      gradients = scaled_gradients
    if self.config.clip_gradients_norm > 0:
      gradients, gnorm = tf.clip_by_global_norm(gradients,
                                                self.config.clip_gradients_norm)
    optimizer_op = self.optimizer.apply_gradients(
        zip(gradients, trainable_vars))
    if self.config.moving_average_decay:
      with tf.control_dependencies([optimizer_op]):
        self.ema.apply(self.trainable_variables)
    return {'loss': total_loss}

  def test_step(self, data):
    """Test step.

    Args:
      data: Tuple of (images, labels). Image tensor with shape [batch_size, height, width, 3].
        The height and width are fixed and equal.Input labels in a dictionary. The labels include class targets
        and box targets which are dense label maps. The labels are generated from
        get_input_fn function in data/dataloader.py.

    Returns:
      A dict record loss info.
    """
    images, labels = data
    cls_outputs, box_outputs = self(images, training=False)
    det_loss, cls_loss, box_loss, box_iou_loss = self._detection_loss(
        cls_outputs, box_outputs, labels)
    reg_l2loss = self._reg_l2_loss(self.config.weight_decay)
    total_loss = det_loss + reg_l2loss
    return {'loss': total_loss}
