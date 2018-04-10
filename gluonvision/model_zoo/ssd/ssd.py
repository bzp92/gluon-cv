"""Single-shot Multi-box Detector."""
from __future__ import absolute_import

from mxnet import autograd
from mxnet.gluon import nn
from mxnet.gluon import HybridBlock
from ..features import FeatureExpander
from .anchor import SSDAnchorGenerator
from ..predictors import ConvPredictor
from ..coders import MultiClassDecoder, NormalizedBoxCenterDecoder
from .target import SSDTargetGenerator
from .vgg_atrous import vgg16_atrous_300, vgg16_atrous_512
from ...utils import set_lr_mult

__all__ = ['ssd_300_vgg16_atrous', 'ssd_512_vgg16_atrous',
           'ssd_512_resnet18_v1', 'ssd_512_resnet50_v1',
           'ssd_512_resnet101_v2', 'ssd_512_resnet152_v2']


class SSD(HybridBlock):
    """Single-shot Object Detection Network: https://arxiv.org/abs/1512.02325.

    Parameters
    ----------
    network : string or None
        Name of the base network, if `None` is used, will instantiate the
        base network from `features` directly instead of composing.
    base_size : int
        Base input size, it is speficied so SSD can support dynamic input shapes.
    features : list of str or mxnet.gluon.HybridBlock
        Intermediate features to be extracted or a network with multi-output.
        If `network` is `None`, `features` is expected to be a multi-output network.
    num_filters : list of int
        Number of channels for the appended layers, ignored if `network`is `None`.
    scale : tuple fo float
        Min and max range of anchors. Suggested value is (0.1, 0.95).
    ratios : iterable of list
        Aspect ratios of anchors in each output layer. Its length must be equals
        to the number of SSD output layers.
    steps : list of int
        Step size of anchor boxes in each output layer.
    classes : int
        Number of categories to be classified. It is 20 for Pascal VOC for example.
    use_1x1_transition : bool
        Whether to use 1x1 convolution as transition layer between attached layers,
        it is effective reducing model capacity.
    use_bn : bool
        Whether to use BatchNorm layer after each attached convolutional layer.
    reduce_ratio : float
        Channel reduce ratio (0, 1) of the transition layer.
    min_depth : int
        Minimum channels for the transition layers.
    global_pool : bool
        Whether to attach a global average pooling layer as the last output layer.
    pretrained : bool
        Description of parameter `pretrained`.
    iou_thresh : float, default is 0.5
        IOU overlap threshold of matching targets, used during training phase.
    neg_thresh : float, default is 0.5
        Negative mining threshold for un-matched anchors, this is to avoid highly
        overlapped anchors to be treated as negative samples.
    negative_mining_ratio : float, default is 3
        Ratio of negative vs. positive samples.
    stds : tuple of float, default is (0.1, 0.1, 0.2, 0.2)
        Std values to be divided/multiplied to box encoded values.
    nms_thresh : float, default is 0.45.
        Non-maximum suppression threshold. You can speficy < 0 or > 1 to disable NMS.
    nms_topk : int, default is -1
        Apply NMS to top k detection results, use -1 to disable so that every Detection
         result is used in NMS.
    force_nms : bool, default is False
        Force suppress objects even they belong to different categories if `True`.
    anchor_alloc_size : tuple of int, default is (128, 128)
        For advanced users. Define `anchor_alloc_size` to generate large enough anchor
        maps, which will later saved in parameters. During inference, we support arbitrary
        input image by cropping corresponding area of the anchor map.

    """
    def __init__(self, network, base_size, features, num_filters, sizes, ratios,
                 steps, classes, use_1x1_transition=True, use_bn=True,
                 reduce_ratio=1.0, min_depth=128, global_pool=False, pretrained=False,
                 iou_thresh=0.5, neg_thresh=0.5, negative_mining_ratio=3,
                 stds=(0.1, 0.1, 0.2, 0.2), nms_thresh=0, nms_topk=-1, force_nms=False,
                 anchor_alloc_size=128, **kwargs):
        super(SSD, self).__init__(**kwargs)
        if network is None:
            num_layers = len(ratios)
        else:
            num_layers = len(features) + len(num_filters) + int(global_pool)
        # assert len(scale) == 2, "Must specify scale as (min_scale, max_scale)."
        # min_scale, max_scale = scale
        # sizes = [min_scale + (max_scale - min_scale) * i / (num_layers - 1)
        #          for i in range(num_layers)] + [1.0]
        # sizes = [x * base_size for x in sizes]
        # sizes = [30, 60, 111, 162, 213, 264, 315]
        assert len(sizes) == num_layers + 1
        sizes = list(zip(sizes[:-1], sizes[1:]))
        assert isinstance(ratios, list), "Must provide ratios as list or list of list"
        if not isinstance(ratios[0], (tuple, list)):
            ratios = ratios * num_layers  # propagate to all layers if use same ratio
        assert num_layers == len(sizes) == len(ratios), \
            "Mismatched (number of layers) vs (sizes) vs (ratios): {}, {}, {}".format(
                num_layers, len(sizes), len(ratios))
        assert num_layers > 0, "SSD require at least one layer, suggest multiple."
        self._num_layers = num_layers
        self.num_classes = classes + 1
        self.nms_thresh = nms_thresh
        self.nms_topk = nms_topk
        self.force_nms = force_nms
        self.target = set([SSDTargetGenerator(
            iou_thresh=iou_thresh, neg_thresh=neg_thresh,
            negative_mining_ratio=negative_mining_ratio, stds=stds)])

        with self.name_scope():
            if network is None:
                # use fine-grained manually designed block as features
                self.features = features(pretrained=pretrained)
            else:
                self.features = FeatureExpander(
                    network=network, outputs=features, num_filters=num_filters,
                    use_1x1_transition=use_1x1_transition,
                    use_bn=use_bn, reduce_ratio=reduce_ratio, min_depth=min_depth,
                    global_pool=global_pool, pretrained=pretrained)
            self.class_predictors = nn.HybridSequential()
            self.box_predictors = nn.HybridSequential()
            self.anchor_generators = nn.HybridSequential()
            asz = (anchor_alloc_size, anchor_alloc_size)
            im_size = (base_size, base_size)
            for i, s, r, st in zip(range(num_layers), sizes, ratios, steps):
                self.anchor_generators.add(SSDAnchorGenerator(i, im_size, s, r, st, asz))
                asz = asz // 2
                num_anchors = self.anchor_generators[-1].num_depth
                self.class_predictors.add(ConvPredictor(num_anchors * self.num_classes))
                self.box_predictors.add(ConvPredictor(num_anchors * 4))
            self.bbox_decoder = NormalizedBoxCenterDecoder(stds)
            self.cls_decoder = MultiClassDecoder()

    def set_nms(self, nms_thresh=0, nms_topk=-1, force_nms=False):
        self.nms_thresh = nms_thresh
        self.nms_topk = nms_topk
        self.force_nms = force_nms

    @property
    def target_generator(self):
        return list(self.target)[0]

    # pylint: disable=arguments-differ
    def hybrid_forward(self, F, x):
        """Hybrid forward"""
        features = self.features(x)
        cls_preds = [F.flatten(F.transpose(cp(feat), (0, 2, 3, 1)))
                     for feat, cp in zip(features, self.class_predictors)]
        box_preds = [F.flatten(F.transpose(bp(feat), (0, 2, 3, 1)))
                     for feat, bp in zip(features, self.box_predictors)]
        anchors = [F.reshape(ag(feat), shape=(1, -1))
                   for feat, ag in zip(features, self.anchor_generators)]
        cls_preds = F.concat(*cls_preds, dim=1).reshape((0, -1, self.num_classes))
        box_preds = F.concat(*box_preds, dim=1).reshape((0, -1, 4))
        anchors = F.concat(*anchors, dim=1).reshape((1, -1, 4))
        if autograd.is_recording():
            return [cls_preds, box_preds, anchors]
        bboxes = self.bbox_decoder(box_preds, anchors)
        # #####
        # import numpy as np
        # cls_conf = F.softmax(cls_preds, axis=-1)
        # for b in range(cls_conf.shape[0]):
        #     rtemp = []
        #     cc = cls_conf[b]
        #     bb = bboxes[b].asnumpy()
        #     for i in range(20):
        #         ccc = cc[:, i+1].asnumpy()
        #         mask = np.where(ccc > 0.01)[0]
        #         if mask.size < 1:
        #             continue
        #         bbb = bb[mask, :]
        #         # print(bbb)
        #         # raise
        #         ss = ccc[mask, np.newaxis]
        #         labels = np.ones(ss.shape) * i
        #         rr = np.hstack((labels, ss, bbb))
        #         r = F.array(rr, ctx=cls_preds.context)
        #         rrr = F.contrib.box_nms(r, overlap_thresh=self.nms_thresh, topk=self.nms_topk,
        #             id_index=0, score_index=1, coord_start=2, force_suppress=self.force_nms)
        #         rrr = rrr.asnumpy()
        #         rrr = rrr[np.where(rrr[:, 0] > -0.5)[0], :]
        #         rtemp.append(rrr)
        #     res = np.vstack(rtemp)
        # result = F.array(res).expand_dims(0)
        #
        # #####
        cls_ids, scores = self.cls_decoder(F.softmax(cls_preds))
        result = F.concat(
            cls_ids.expand_dims(axis=-1), scores.expand_dims(axis=-1), bboxes, dim=-1)
        conf_mask = F.tile(scores.expand_dims(axis=-1) > 0.01, reps=(1, 1, 6))
        result = F.where(conf_mask, result, F.ones_like(result) * -1)
        if self.nms_thresh > 0 and self.nms_thresh < 1:
            result = F.contrib.box_nms(
                result, overlap_thresh=self.nms_thresh, topk=self.nms_topk,
                id_index=0, score_index=1, coord_start=2, force_suppress=self.force_nms)
        ids = F.slice_axis(result, axis=2, begin=0, end=1)
        scores = F.slice_axis(result, axis=2, begin=1, end=2)
        bboxes = F.slice_axis(result, axis=2, begin=2, end=6)
        return ids, scores, bboxes

def get_ssd(name, base_size, features, filters, sizes, ratios, steps,
            classes=20, pretrained=0, **kwargs):
    """Get SSD models.

    Parameters
    ----------
    name : str
        Model name
    base_size : int

    """
    net = SSD(name, base_size, features, filters, sizes, ratios, steps,
              pretrained=pretrained > 0, classes=classes, **kwargs)
    if pretrained > 1:
        # load trained ssd model
        raise NotImplementedError("Loading pretrained model for detection is not finished.")
    set_lr_mult(net, ".*_bias", 2.0)  #TODO(zhreshold): fix pattern
    return net

def ssd_300_vgg16_atrous(pretrained=0, classes=20, **kwargs):
    """SSD architecture with VGG16 atrous 300x300 base network."""
    net = get_ssd(None, 300, features=vgg16_atrous_300, filters=None,
                  sizes=[30, 60, 111, 162, 213, 264, 315],
                  ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 3 + [[1, 2, 0.5]] * 2,
                  steps=[8, 16, 32, 64, 100, 300],
                  classes=classes, pretrained=pretrained, **kwargs)
    return net

def ssd_512_vgg16_atrous(pretrained=0, classes=20, **kwargs):
    """SSD architecture with VGG16 atrous 512x512 base network."""
    net = get_ssd(None, 512, features=vgg16_atrous_512, filters=None,
                  sizes=[35.84, 76.8, 153.6, 230.4, 307.2, 384.0, 460.8, 537.6],
                  ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 4 + [[1, 2, 0.5]] * 2,
                  steps=[8, 16, 32, 64, 128, 256, 512],
                  classes=classes, pretrained=pretrained, **kwargs)
    return net

def ssd_512_resnet18_v1(pretrained=0, classes=20, **kwargs):
    """SSD architecture with ResNet v1 18 layers."""
    return get_ssd('resnet18_v1', 512,
                   features=['stage3_activation1', 'stage4_activation1'],
                   filters=[512, 512, 256, 256],
                   sizes=[35.84, 76.8, 153.6, 230.4, 307.2, 384.0, 460.8, 537.6],
                   ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 3 + [[1, 2, 0.5]] * 2,
                   steps=[8, 16, 32, 64, 128, 256, 512],
                   classes=classes, pretrained=pretrained, **kwargs)

def ssd_512_resnet50_v1(pretrained=0, classes=20, **kwargs):
    """SSD architecture with ResNet v1 50 layers."""
    return get_ssd('resnet50_v1', 512,
                   features=['stage3_activation5', 'stage4_activation2'],
                   filters=[512, 512, 256, 256],
                   sizes=[35.84, 76.8, 153.6, 230.4, 307.2, 384.0, 460.8, 537.6],
                   ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 3 + [[1, 2, 0.5]] * 2,
                   steps=[16, 32, 64, 128, 256, 512],
                   classes=classes, pretrained=pretrained, **kwargs)

def ssd_512_resnet101_v2(pretrained=0, classes=20, **kwargs):
    """SSD architecture with ResNet v2 101 layers."""
    return get_ssd('resnet101_v2', 512,
                   features=['stage3_activation22', 'stage4_activation2'],
                   filters=[512, 512, 256, 256],
                   sizes=[35.84, 76.8, 153.6, 230.4, 307.2, 384.0, 460.8, 537.6],
                   ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 3 + [[1, 2, 0.5]] * 2,
                   steps=[16, 32, 64, 128, 256, 512],
                   classes=classes, pretrained=pretrained, **kwargs)

def ssd_512_resnet152_v2(pretrained=0, classes=20, **kwargs):
    """SSD architecture with ResNet v2 152 layers."""
    return get_ssd('resnet152_v2', 512,
                   features=['stage2_activation7', 'stage3_activation35', 'stage4_activation2'],
                   filters=[512, 512, 256, 256],
                   sizes=[35.84, 76.8, 153.6, 230.4, 307.2, 384.0, 460.8, 537.6],
                   ratios=[[1, 2, 0.5]] + [[1, 2, 0.5, 3, 1.0/3]] * 4 + [[1, 2, 0.5]] * 2,
                   steps=[8, 16, 32, 64, 128, 256, 512],
                   classes=classes, pretrained=pretrained, **kwargs)
