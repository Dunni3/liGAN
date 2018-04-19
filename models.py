import caffe_util
from caffe import TRAIN, TEST, params


def make_model(encode_type, data_dim, resolution, n_levels, conv_per_level, n_filters, growth_factor, loss_types='',
               molgrid_data=True, batch_size=50, conv_kernel_size=3, pool_type='a', depool_type='n'):

    assert encode_type in ['a', 'c']
    assert pool_type in ['c', 'm', 'a']
    assert depool_type in ['c', 'n']

    n_rec_channels = 16
    n_lig_channels = 19
    n_channels = n_rec_channels + n_lig_channels

    net = caffe_util.NetParameter()

    # input
    if molgrid_data:
        for training in [True, False]:

            data_layer = net.layer.add()
            data_layer.update(name='data',
                              type='MolGridData',
                              top=['data', 'label', 'aff'],
                              include=[dict(phase=TRAIN if training else TEST)])

            data_param = data_layer.molgrid_data_param
            data_param.update(source='TRAINFILE' if training else 'TESTFILE',
                              root_folder='DATA_ROOT',
                              has_affinity=True,
                              batch_size=batch_size,
                              dimension=(data_dim - 1)*resolution,
                              resolution=resolution,
                              shuffle=training,
                              balanced=False,
                              random_rotation=training,
                              random_translate=training*2.0)

        net.layer.add().update(name='no_label', type='Silence', bottom=['label'])
        net.layer.add().update(name='no_aff', type='Silence', bottom=['aff'])

        if encode_type == 'c':

            slice_layer = net.layer.add()
            slice_layer.update(name='slice_rec_lig',
                               type='Slice',
                               bottom=['data'],
                               top=['rec', 'lig'],
                               slice_param=dict(axis=1, slice_point=[n_rec_channels]))
    else:
        if encode_type == 'a':

            data_layer = net.layer.add()
            data_layer.update(name='data',
                              type='Input',
                              top=['data'])
            data_layer.input_param.shape.update(dim=[batch_size, n_channels,
                                                     data_dim, data_dim, data_dim])

        elif encode_type == 'c':

            rec_layer = net.layer.add()
            rec_layer.update(name='rec',
                             type='Input',
                             top=['rec'])
            rec_layer.input_param.shape.update(dim=[batch_size, n_rec_channels,
                                                    data_dim, data_dim, data_dim])

            lig_layer = net.layer.add()
            lig_layer.update(name='lig',
                             type='Input',
                             top=['lig'])
            lig_layer.input_param.shape.update(dim=[batch_size, n_lig_channels,
                                                    data_dim, data_dim, data_dim])

    if encode_type == 'a':
        curr_top = 'data'
        curr_n_filters = n_channels
        label_top = 'data'
        label_n_filters = n_channels

    elif encode_type == 'c':
        curr_top = 'rec'
        curr_n_filters = n_rec_channels
        label_top = 'lig'
        label_n_filters = n_lig_channels

    curr_dim = data_dim
    next_n_filters = n_filters

    # encoder
    for i in range(n_levels):

        if i > 0: # pool before convolution

            pool_name = 'level{}_pool'.format(i)
            pool_layer = net.layer.add()
            pool_layer.update(name=pool_name,
                              bottom=[curr_top],
                              top=[pool_name])

            if pool_type == 'c': # convolution with stride 2

                pool_layer.type = 'Convolution'
                pool_param = pool_layer.convolution_param
                pool_param.update(num_output=curr_n_filters,
                                  group=curr_n_filters,
                                  weight_filler=dict(type='xavier'))

            elif pool_type == 'm': # max pooling

                pool_layer.type = 'Pooling'
                pool_param = pool_layer.pooling_param
                pool_param.pool = params.Pooling.MAX

            elif pool_type == 'a': # average pooling

                pool_layer.type = 'Pooling'
                pool_param = pool_layer.pooling_param
                pool_param.pool = params.Pooling.AVE

            curr_top = pool_name
            first_depool_dim = curr_dim
            
            if curr_dim % 2 == 0:
                pool_param.update(kernel_size=[2], stride=[2], pad=[0])
                curr_dim /= 2

            else:
                pool_param.update(kernel_size=[curr_dim], stride=[1], pad=[0])
                curr_dim = 1

            next_n_filters *= growth_factor
        
        for j in range(conv_per_level): # convolutions

            conv_name = 'level{}_conv{}'.format(i, j)
            conv_layer = net.layer.add()
            conv_layer.update(name=conv_name,
                              type='Convolution',
                              bottom=[curr_top],
                              top=[conv_name])

            conv_param = conv_layer.convolution_param
            conv_param.update(num_output=next_n_filters,
                              kernel_size=[conv_kernel_size],
                              stride=[1],
                              pad=[conv_kernel_size//2],
                              weight_filler=dict(type='xavier'))

            relu_name = 'level{}_relu{}'.format(i, j)
            relu_layer = net.layer.add()
            relu_layer.update(name=relu_name,
                              type='ReLU',
                              bottom=[conv_name],
                              top=[conv_name])
            relu_layer.relu_param.negative_slope = 0.0

            curr_top = conv_name
            curr_n_filters = next_n_filters

    next_dim = first_depool_dim

    print(batch_size, curr_n_filters, curr_dim, curr_dim, curr_dim)
    print(curr_n_filters*curr_dim**3)

    # decoder
    for i in reversed(range(n_levels)):

        if i < n_levels-1: # upsample before convolution

            depool_name = 'level{}_depool'.format(i)
            depool_layer = net.layer.add()
            depool_layer.update(name=depool_name,
                                bottom=[curr_top],
                                top=[depool_name])

            if depool_type == 'c': # deconvolution with stride 2

                depool_layer.type = 'Deconvolution'
                depool_param = depool_layer.convolution_param
                depool_param.update(num_output=curr_n_filters,
                                    group=curr_n_filters,
                                    weight_filler=dict(type='xavier'))

            elif depool_type == 'n': # nearest-neighbor interpolation

                depool_layer.type = 'Deconvolution'
                depool_layer.update(param=[dict(lr_mult=0.0, decay_mult=0.0)])
                depool_param = depool_layer.convolution_param
                depool_param.update(num_output=curr_n_filters,
                                    group=curr_n_filters,
                                    weight_filler=dict(type='constant', value=1.0),
                                    bias_term=False)

            curr_top = depool_name
            curr_dim = first_depool_dim
            
            if curr_dim % 2 == 0:
                depool_param.update(kernel_size=[2], stride=[2], pad=[0])

            else:
                depool_param.update(kernel_size=[curr_dim], stride=[1], pad=[0])

            next_dim *= 2
            next_n_filters /= growth_factor

        for j in range(conv_per_level): # convolutions

            last_conv = i == 0 and j+1 == conv_per_level

            if last_conv:
                next_n_filters = label_n_filters

            deconv_name = 'level{}_deconv{}'.format(i, j)
            deconv_layer = net.layer.add()
            deconv_layer.update(name=deconv_name,
                                type='Deconvolution',
                                bottom=[curr_top],
                                top=[deconv_name])

            deconv_param = deconv_layer.convolution_param
            deconv_param.update(num_output=next_n_filters,
                                kernel_size=[conv_kernel_size],
                                stride=[1],
                                pad=[conv_kernel_size//2],
                                weight_filler=dict(type='xavier'))

            derelu_name = 'level{}_derelu{}'.format(i, j)
            derelu_layer = net.layer.add()
            derelu_layer.update(name=derelu_name,
                                type='ReLU',
                                bottom=[deconv_name],
                                top=[deconv_name])
            derelu_layer.relu_param.negative_slope = 0.0

            curr_top = deconv_name
            curr_n_filters = next_n_filters

    pred_top = curr_top

    # loss
    if 'e' in loss_types:

        loss_name = 'l2_loss'
        loss_layer = net.layer.add()
        loss_layer.update(name=loss_name,
                          type='EuclideanLoss',
                          bottom=[pred_top, label_top],
                          top=[loss_name],
                          loss_weight=[1.0])

    return net


if __name__ == '__main__':
    net_param = make_model('c', 24, 0.5, 2, 3, 64, 2, 'e')
