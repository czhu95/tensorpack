#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# File: cifar10-resnet.py
# Author: Yuxin Wu <ppwwyyxx@gmail.com>

import numpy as np
import tensorflow as tf
import argparse
import os

from tensorpack import *
from tensorpack.tfutils.symbolic_functions import *
from tensorpack.tfutils.summary import *
from dorefa import get_dorefa

"""
CIFAR10 ResNet example. See:
Deep Residual Learning for Image Recognition, arxiv:1512.03385
This implementation uses the variants proposed in:
Identity Mappings in Deep Residual Networks, arxiv:1603.05027

I can reproduce the results on 2 TitanX for
n=5, about 7.1% val error after 67k step (8.6 step/s)
n=18, about 5.7% val error (2.45 step/s)
n=30: a 182-layer network, about 5.6% val error after 51k step (1.55 step/s)
This model uses the whole training set instead of a train-val split.
"""

BATCH_SIZE = 128

class Model(ModelDesc):
    def __init__(self, n):
        super(Model, self).__init__()
        self.n = n

    def _get_input_vars(self):
        return [InputVar(tf.float32, [None, 32, 32, 3], 'input'),
                InputVar(tf.int32, [None], 'label')
               ]

    def _build_graph(self, input_vars, is_training):
        image, label = input_vars
        image = image / 128.0 - 1
        fw, fa, fg = get_dorefa(BITW, BITA, BITG)
        # monkey-patch tf.get_variable to apply fw
        old_get_variable = tf.get_variable
        def new_get_variable(name, shape=None, **kwargs):
            v = old_get_variable(name, shape, **kwargs)
            # don't binarize first and last layer
            if name != 'W' or 'conv0' in v.op.name or 'fct' in v.op.name:
                return v
            else:
                logger.info("Binarizing weight {}".format(v.op.name))
                return fw(v)
        tf.get_variable = new_get_variable

        def nonlin(x):
            if BITA == 32:
                return tf.nn.relu(x)    # still use relu for 32bit cases
            return tf.clip_by_value(x, 0.0, 1.0)

        def activate(x):
            return fa(nonlin(x))

        def conv(name, l, channel, stride):
            return Conv2D(name, l, channel, 3, stride=stride,
                          nl=tf.identity, use_bias=False,
                          W_init=tf.random_normal_initializer(stddev=np.sqrt(2.0/9/channel)))

        def residual(name, l, increase_dim=False, first=False):
            shape = l.get_shape().as_list()
            in_channel = shape[3]

            if increase_dim:
                out_channel = in_channel * 2
                stride1 = 2
            else:
                out_channel = in_channel
                stride1 = 1

            with tf.variable_scope(name) as scope:
                if not first:
                    b1 = BatchNorm('bn1', l, is_training)
                    b1 = tf.nn.relu(b1)
                else:
                    b1 = l
                c1 = conv('conv1', b1, out_channel, stride1)
                c1 = fg(c1)
                b2 = BatchNorm('bn2', c1, is_training)
                b2 = activate(b2)
                c2 = conv('conv2', b2, out_channel, 1)
                c2 = activate(c2)

                if increase_dim:
                    l = AvgPooling('pool', l, 2)
                    l = tf.pad(l, [[0,0], [0,0], [0,0], [in_channel//2, in_channel//2]])

                l = c2 + l
                return l

        l = conv('conv0', image, 16, 1)
        l = BatchNorm('bn0', l, is_training)
        l = activate(l)
        l = residual('res1.0', l, first=True)
        for k in range(1, self.n):
            l = residual('res1.{}'.format(k), l)
        # 32,c=16

        l = residual('res2.0', l, increase_dim=True)
        for k in range(1, self.n):
            l = residual('res2.{}'.format(k), l)
        # 16,c=32

        l = residual('res3.0', l, increase_dim=True)
        for k in range(1, self.n):
            l = residual('res3.' + str(k), l)
        l = BatchNorm('bnlast', l, is_training)
        l = nonlin(l)
        # 8,c=64
        l = GlobalAvgPooling('gap', l)
        logits = FullyConnected('linear', l, out_dim=10, nl=tf.identity)
        prob = tf.nn.softmax(logits, name='output')

        cost = tf.nn.sparse_softmax_cross_entropy_with_logits(logits, label)
        cost = tf.reduce_mean(cost, name='cross_entropy_loss')

        wrong = prediction_incorrect(logits, label)
        nr_wrong = tf.reduce_sum(wrong, name='wrong')
        # monitor training error
        add_moving_summary(tf.reduce_mean(wrong, name='train_error'))

        # weight decay on all W of fc layers
        wd_w = tf.train.exponential_decay(0.0002, get_global_step_var(),
                                          480000, 0.2, True)
        wd_cost = tf.mul(wd_w, regularize_cost('.*/W', tf.nn.l2_loss), name='wd_cost')
        add_moving_summary(cost, wd_cost)

        add_param_summary([('.*/W', ['histogram'])])   # monitor W
        self.cost = tf.add_n([cost, wd_cost], name='cost')

def get_data(train_or_test):
    isTrain = train_or_test == 'train'
    ds = dataset.Cifar10(train_or_test)
    pp_mean = ds.get_per_pixel_mean()
    if isTrain:
        augmentors = [
            imgaug.CenterPaste((40, 40)),
            imgaug.RandomCrop((32, 32)),
            imgaug.Flip(horiz=True),
            #imgaug.Brightness(20),
            #imgaug.Contrast((0.6,1.4)),
            imgaug.MapImage(lambda x: x - pp_mean),
        ]
    else:
        augmentors = [
            imgaug.MapImage(lambda x: x - pp_mean)
        ]
    ds = AugmentImageComponent(ds, augmentors)
    ds = BatchData(ds, 128, remainder=not isTrain)
    if isTrain:
        ds = PrefetchData(ds, 3, 2)
    return ds

def get_config():
    logger.auto_set_dir()

    # prepare dataset
    dataset_train = get_data('train')
    step_per_epoch = dataset_train.size()
    dataset_test = get_data('test')

    sess_config = get_default_sess_config(0.9)

    get_global_step_var()
    lr = tf.Variable(0.01, trainable=False, name='learning_rate')
    tf.scalar_summary('learning_rate', lr)

    return TrainConfig(
        dataset=dataset_train,
        optimizer=tf.train.MomentumOptimizer(lr, 0.9),
        callbacks=Callbacks([
            StatPrinter(),
            ModelSaver(),
            InferenceRunner(dataset_test,
                [ScalarStats('cost'), ClassificationError()]),
            ScheduledHyperParamSetter('learning_rate',
                                      [(1, 0.1), (82, 0.01), (123, 0.001), (300, 0.0002)])
        ]),
        session_config=sess_config,
        model=Model(n=18),
        step_per_epoch=step_per_epoch,
        max_epoch=400,
    )

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.') # nargs='*' in multi mode
    parser.add_argument('--load', help='load model')
    parser.add_argument('--dorefa',
            help='number of bits for W,A,G, separated by comma. Defaults to \'1,2,4\'',
            default='1,2,4')
    args = parser.parse_args()

    BITW, BITA, BITG = map(int, args.dorefa.split(','))

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    config = get_config()
    if args.load:
        config.session_init = SaverRestore(args.load)
    if args.gpu:
        config.nr_tower = len(args.gpu.split(','))
    SyncMultiGPUTrainer(config).train()