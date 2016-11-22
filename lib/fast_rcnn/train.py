#coding:utf8
# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""Train a Fast R-CNN network."""

from fast_rcnn.config import cfg
import gt_data_layer.roidb as gdl_roidb
import roi_data_layer.roidb as rdl_roidb
from roi_data_layer.layer import RoIDataLayer
from utils.timer import Timer
import numpy as np
import os
import tensorflow as tf
import sys
import logging

class SolverWrapper(object):
    """A simple wrapper around Caffe's solver.
    This wrapper gives us control over he snapshotting process, which we
    use to unnormalize the learned bounding-box regression weights.
    """

    def __init__(self, sess, network, imdb, roidb, output_dir, pretrained_model=None, checkpoint=None):
        """Initialize the SolverWrapper."""
        self.net = network
        self.imdb = imdb
        self.roidb = roidb
        self.output_dir = output_dir
        self.pretrained_model = pretrained_model
        self.checkpoint = checkpoint
        if self.pretrained_model is not None and self.checkpoint is not None:
            raise Exception('Please do not give pretrained_model and checkpoint together!')
        if cfg.TRAIN.BBOX_REG:
            self.bbox_means, self.bbox_stds = rdl_roidb.add_bbox_regression_targets(roidb)
        """保存这个信息是给预测用的。原先的机制是unnormalize然后保存的。原先的机制会导致无法方便地使用checkpoint进行训练。"""
        with tf.variable_scope('custom', reuse=False):
            bbox_means = tf.get_variable("bbox_means", self.bbox_means.shape, trainable=False)
            bbox_stds = tf.get_variable("bbox_stds", self.bbox_stds.shape, trainable=False)
        self.global_step = tf.Variable(0, trainable=False)
        # For checkpoint
        self.saver = tf.train.Saver(max_to_keep=100)


    def snapshot(self, sess, iter):
        """Take a snapshot of the network after unnormalizing the learned
        bounding-box regression weights. This enables easy use at test-time.
        """
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        infix = ('_' + cfg.TRAIN.SNAPSHOT_INFIX
                 if cfg.TRAIN.SNAPSHOT_INFIX != '' else '')
        filename = (cfg.TRAIN.SNAPSHOT_PREFIX + infix +
                    '_iter_{:d}'.format(iter + 1) + '.ckpt')
        filename = os.path.join(self.output_dir, filename)

        self.saver.save(sess, filename)
        print 'Wrote snapshot to: {:s}'.format(filename)


    def train_model(self, sess, max_iters):
        """Network training loop."""

        data_layer = get_data_layer(self.roidb, self.imdb.num_classes)

        # RPN
        # classification loss
        rpn_cls_score = tf.reshape(self.net.get_output('rpn_cls_score_reshape'), [-1, 2])
        rpn_label = tf.reshape(self.net.get_output('rpn-data')[0], [-1])
        # ignore_label(-1)
        rpn_cls_score = tf.reshape(tf.gather(rpn_cls_score, tf.where(tf.not_equal(rpn_label, -1))), [-1, 2])
        rpn_label = tf.reshape(tf.gather(rpn_label, tf.where(tf.not_equal(rpn_label, -1))), [-1])
        rpn_cross_entropy = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(rpn_cls_score, rpn_label))

        # bounding box regression L1 loss
        rpn_bbox_pred = self.net.get_output('rpn_bbox_pred')
        rpn_bbox_targets = tf.transpose(self.net.get_output('rpn-data')[1], [0, 2, 3, 1])
        rpn_bbox_inside_weights = tf.transpose(self.net.get_output('rpn-data')[2], [0, 2, 3, 1])
        rpn_bbox_outside_weights = tf.transpose(self.net.get_output('rpn-data')[3], [0, 2, 3, 1])
        smoothL1_sign = tf.cast(tf.less(tf.abs(tf.sub(rpn_bbox_pred, rpn_bbox_targets)), 1), tf.float32)
        rpn_loss_box = tf.mul(tf.reduce_mean(tf.reduce_sum(tf.mul(rpn_bbox_outside_weights, tf.add(
            tf.mul(tf.mul(tf.pow(tf.mul(rpn_bbox_inside_weights, tf.sub(rpn_bbox_pred, rpn_bbox_targets)) * 3, 2), 0.5), smoothL1_sign),
            tf.mul(tf.sub(tf.abs(tf.sub(rpn_bbox_pred, rpn_bbox_targets)), 0.5 / 9.0), tf.abs(smoothL1_sign - 1)))), reduction_indices=[1, 2])), 10)

        # R-CNN
        # classification loss
        cls_score = self.net.get_output('cls_score')
        # label = tf.placeholder(tf.int32, shape=[None])
        label = tf.reshape(self.net.get_output('roi-data')[1], [-1])
        cross_entropy = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(cls_score, label))

        # bounding box regression L1 loss
        bbox_pred = self.net.get_output('bbox_pred')
        bbox_targets = self.net.get_output('roi-data')[2]
        bbox_inside_weights = self.net.get_output('roi-data')[3]
        bbox_outside_weights = self.net.get_output('roi-data')[4]
        loss_box = tf.reduce_mean(tf.reduce_sum(tf.mul(bbox_outside_weights, tf.mul(bbox_inside_weights, tf.abs(tf.sub(bbox_pred, bbox_targets)))), reduction_indices=[1]))

        loss = cross_entropy + loss_box + rpn_cross_entropy + rpn_loss_box

        # optimizer
        lr = tf.train.exponential_decay(cfg.TRAIN.LEARNING_RATE, self.global_step,
                                        cfg.TRAIN.STEPSIZE, cfg.TRAIN.LR_DECAY_RATE, staircase=True)
        momentum = cfg.TRAIN.MOMENTUM
        train_op = tf.train.MomentumOptimizer(lr, momentum).minimize(loss, global_step=self.global_step)

        # iintialize variables
        sess.run(tf.initialize_all_variables())

        """在这里赋值，如果在构造函数内赋值，会被这个变量初始化覆盖"""
        with tf.variable_scope('custom', reuse=True):
            bbox_means = tf.get_variable("bbox_means")
            bbox_stds = tf.get_variable("bbox_stds")
            sess.run(bbox_means.assign(self.bbox_means))
            sess.run(bbox_stds.assign(self.bbox_stds))

        if self.pretrained_model is not None:
            print ('Loading pretrained model '
                   'weights from {:s}').format(self.pretrained_model)
            self.net.load(self.pretrained_model, sess, True)
        elif self.checkpoint is not None:
            print('Loading checkpoint from: ' + self.checkpoint)
            self.saver.restore(sess, self.checkpoint)

        start_global_step = int(sess.run(self.global_step))
        last_snapshot_iter = -1
        timer = Timer()
        for iter in range(start_global_step, max_iters):
            timer.tic()
            # get one batch
            blobs = data_layer.forward()

            # Make one SGD update
            feed_dict = {self.net.data: blobs['data'], self.net.im_info: blobs['im_info'], self.net.keep_prob: 0.5,
                         self.net.gt_boxes: blobs['gt_boxes']}

            rpn_loss_cls_value, rpn_loss_box_value, loss_cls_value, loss_box_value, _ = sess.run([rpn_cross_entropy, rpn_loss_box, cross_entropy, loss_box, train_op], feed_dict=feed_dict)

            timer.toc()

            if (iter + 1) % (cfg.TRAIN.DISPLAY) == 0:
                print 'iter: %d / %d,total loss: %.4f, rpn_loss_cls: %.4f, rpn_loss_box: %.4f, loss_cls: %.4f, loss_box: %.4f, lr: %f' %\
                    (iter + 1, max_iters, rpn_loss_cls_value + rpn_loss_box_value + loss_cls_value + loss_box_value, rpn_loss_cls_value, rpn_loss_box_value, loss_cls_value, loss_box_value, lr.eval())
                print 'speed: {:.3f}s / iter'.format(timer.average_time)

            if (iter + 1) % cfg.TRAIN.SNAPSHOT_ITERS == 0:
                last_snapshot_iter = iter
                self.snapshot(sess, iter)

        if last_snapshot_iter != iter:
            self.snapshot(sess, iter)


def get_training_roidb(imdb):
    """Returns a roidb (Region of Interest database) for use in training."""
    if cfg.TRAIN.USE_FLIPPED:
        print 'Appending horizontally-flipped training examples...'
        imdb.append_flipped_images()
        print 'done'

    print 'Preparing training data...'
    if cfg.TRAIN.HAS_RPN:
        if cfg.IS_MULTISCALE:
            gdl_roidb.prepare_roidb(imdb)
        else:
            rdl_roidb.prepare_roidb(imdb)
    else:
        rdl_roidb.prepare_roidb(imdb)
    print 'done'

    return imdb.roidb


def get_data_layer(roidb, num_classes):
    """return a data layer."""
    if cfg.TRAIN.HAS_RPN:
        if cfg.IS_MULTISCALE:
            layer = GtDataLayer(roidb)
        else:
            layer = RoIDataLayer(roidb, num_classes)
    else:
        layer = RoIDataLayer(roidb, num_classes)

    return layer


def train_net(network, imdb, roidb, output_dir, pretrained_model=None, max_iters=40000, checkpoint=None):
    """Train a Fast R-CNN network."""

    with tf.Session(config=tf.ConfigProto(allow_soft_placement=True)) as sess:
        sw = SolverWrapper(sess, network, imdb, roidb, output_dir, pretrained_model=pretrained_model, checkpoint=checkpoint)
        print 'Solving...'
        sw.train_model(sess, max_iters)
        print 'done solving'
