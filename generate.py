from __future__ import print_function
import sys, os, re, argparse, time, glob, struct, time
import datetime as dt
import numpy as np
import pandas as pd
import scipy as sp
from collections import defaultdict, Counter
import multiprocessing as mp
import threading
import contextlib
import tempfile
from itertools import izip
from functools import partial
from scipy.stats import multivariate_normal
import caffe
import openbabel as ob
import pybel
import caffe_util
import atom_types
pd.set_option('display.width', 250)


def get_atom_density(atom_pos, atom_radius, points, radius_multiple):
    '''
    Compute the density value of an atom at a set of points.
    '''
    dist2 = np.sum((points - atom_pos)**2, axis=1)
    dist = np.sqrt(dist2)
    h = 0.5*atom_radius
    ie2 = np.exp(-2)
    zero_cond = dist >= radius_multiple * atom_radius
    gauss_cond = dist <= atom_radius
    gauss_val = np.exp(-dist2 / (2*h**2))
    quad_val = dist2*ie2/(h**2) - 6*dist*ie2/h + 9*ie2
    return np.where(zero_cond, 0.0, np.where(gauss_cond, gauss_val, quad_val))


def get_atom_density2(atom_pos, atom_radius, points, radius_multiple):
    return np.exp(-2*np.sum((points - atom_pos)**2, axis=1)/atom_radius**2)


def get_atom_gradient(atom_pos, atom_radius, points, radius_multiple):
    '''
    Compute the derivative of an atom's density with respect
    to a set of points.
    '''
    diff = points - atom_pos
    dist2 = np.sum(diff**2, axis=1)
    dist = np.sqrt(dist2)
    h = 0.5*atom_radius
    ie2 = np.exp(-2)
    zero_cond = np.logical_or(dist >= radius_multiple * atom_radius, np.isclose(dist, 0))
    gauss_cond = dist <= atom_radius
    gauss_val = -dist / h**2 * np.exp(-dist2 / (2*h**2))
    quad_val = 2*dist*ie2/(h**2) - 6*ie2/h
    return -diff * np.where(zero_cond, 0.0, np.where(gauss_cond, gauss_val, quad_val) / dist)[:,np.newaxis]


def get_bond_length_energy(distance, bond_length, bonds):
    '''
    Compute the interatomic potential energy between an atom and a set of atoms.
    '''
    exp = np.exp(bond_length - distance)
    return (1 - exp)**2 * bonds


def get_bond_length_gradient(distance, bond_length, bonds):
    '''
    Compute the derivative of interatomic potential energy between an atom
    and a set of atoms with respect to the position of the first atom.
    '''
    exp = np.exp(bond_length - distance)
    return 2 * (1 - exp) * exp * bonds
    return (-diff * (d_energy / dist)[:,np.newaxis])


def fit_atoms_by_GMM(points, density, xyz_init, atom_radius, radius_multiple, max_iter, 
                     noise_model='', noise_params_init={}, gof_crit='nll', verbose=0):
    '''
    Fit atom positions to a set of points with the given density values with
    a Gaussian mixture model (and optional noise model). Return the final atom
    positions and a goodness-of-fit criterion (negative log likelihood, Akaike
    information criterion, or L2 loss).
    '''
    assert gof_crit in {'nll', 'aic', 'L2'}, 'Invalid value for gof_crit argument'
    n_points = len(points)
    n_atoms = len(xyz_init)
    xyz = np.array(xyz_init)
    atom_radius = np.array(atom_radius)
    cov = (0.5*atom_radius)**2
    n_params = xyz.size

    assert noise_model in {'d', 'p', ''}, 'Invalid value for noise_model argument'
    if noise_model == 'd':
        noise_mean = noise_params_init['mean']
        noise_cov = noise_params_init['cov']
        n_params += 2
    elif noise_model == 'p':
        noise_prob = noise_params_init['prob']
        n_params += 1

    # initialize uniform prior over components
    n_comps = n_atoms + bool(noise_model)
    assert n_comps > 0, 'Need at least one component (atom or noise model) to fit GMM'
    P_comp = np.full(n_comps, 1.0/n_comps) # P(comp_j)
    n_params += n_comps - 1

    # maximize expected log likelihood
    ll = -np.inf
    i = 0
    while True:

        L_point = np.zeros((n_points, n_comps)) # P(point_i|comp_j)
        for j in range(n_atoms):
            L_point[:,j] = multivariate_normal.pdf(points, mean=xyz[j], cov=cov[j])
        if noise_model == 'd':
            L_point[:,-1] = multivariate_normal.pdf(density, mean=noise_mean, cov=noise_cov)
        elif noise_model == 'p':
            L_point[:,-1] = noise_prob

        P_joint = P_comp * L_point          # P(point_i, comp_j)
        P_point = np.sum(P_joint, axis=1)   # P(point_i)
        gamma = (P_joint.T / P_point).T     # P(comp_j|point_i) (E-step)

        # compute expected log likelihood
        ll_prev, ll = ll, np.sum(density * np.log(P_point))
        if ll - ll_prev < 1e-3 or i == max_iter:
            break

        # estimate parameters that maximize expected log likelihood (M-step)
        for j in range(n_atoms):
            xyz[j] = np.sum(density * gamma[:,j] * points.T, axis=1) \
                   / np.sum(density * gamma[:,j])
        if noise_model == 'd':
            noise_mean = np.sum(gamma[:,-1] * density) / np.sum(gamma[:,-1])
            noise_cov = np.sum(gamma[:,-1] * (density - noise_mean)**2) / np.sum(gamma[:,-1])
            if noise_cov == 0.0 or np.isnan(noise_cov): # reset noise
                noise_mean = noise_params_init['mean']
                noise_cov = noise_params_init['cov']
        elif noise_model == 'p':
            noise_prob = noise_prob
        if noise_model and n_atoms > 0:
            P_comp[-1] = np.sum(density * gamma[:,-1]) / np.sum(density)
            P_comp[:-1] = (1.0 - P_comp[-1])/n_atoms
        i += 1
        if verbose > 2:
            print('iteration = {}, nll = {} ({})'.format(i, -ll, -(ll - ll_prev)), file=sys.stderr)

    # compute the goodness-of-fit
    if gof_crit == 'L2':
        density_pred = np.zeros_like(density)
        for j in range(n_atoms):
            density_pred += get_atom_density(xyz[j], atom_radius[j], points, radius_multiple)
        gof = np.sum((density_pred - density)**2)/2
    elif gof_crit == 'aic':
        gof = 2*n_params - 2*ll
    else:
        gof = -ll

    return xyz, gof


def conv_grid(grid, kernel):
    # convolution theorem: g * grid = F-1(F(g)F(grid))
    F_h = np.fft.fftn(kernel)
    F_grid = np.fft.fftn(grid)
    return np.real(np.fft.ifftn(F_grid * F_h))


def wiener_deconv_grid(grid, kernel, noise_ratio=0.0):
    '''
    Applies a convolution to the input grid that approximates the inverse
    of the operation that converts a set of atom positions to a grid of
    atom density.
    '''
    # we want a convolution g such that g * grid = a, where a is the atom positions
    # we assume that grid = h * a, so g is the inverse of h: g * (h * a) = a
    # take F() to be the Fourier transform, F-1() the inverse Fourier transform
    # convolution theorem: g * grid = F-1(F(g)F(grid))
    # Wiener deconvolution: F(g) = 1/F(h) |F(h)|^2 / (|F(h)|^2 + noise_ratio)
    F_h = np.fft.fftn(kernel)
    F_grid = np.fft.fftn(grid)
    conj_F_h = np.conj(F_h)
    F_g = conj_F_h / (F_h*conj_F_h + noise_ratio)
    return np.real(np.fft.ifftn(F_grid * F_g))


def wiener_deconv_grids(grids, channels, resolution, radius_multiple, noise_ratio=0.0, radius_factor=1.0):

    deconv_grids = np.zeros_like(grids)
    points = get_grid_points(grids.shape[1:], 0, resolution)

    for i, grid in enumerate(grids):

        r = channels[i].atomic_radius*radius_factor
        kernel = get_atom_density(resolution/2, r, points, radius_multiple).reshape(grid.shape)
        kernel = np.roll(kernel, shift=[d//2 for d in grid.shape], axis=range(grid.ndim))
        deconv_grids[i,...] = wiener_deconv_grid(grid, kernel, noise_ratio)

    return np.stack(deconv_grids, axis=0)


def get_grid_points(shape, center, resolution):
    '''
    Return an array of grid points with a certain shape.
    '''
    shape = np.array(shape)
    center = np.array(center)
    resolution = np.array(resolution)
    origin = center - resolution*(shape - 1)/2.0
    indices = np.array(list(np.ndindex(*shape)))
    return origin + resolution*indices


def grid_to_points_and_values(grid, center, resolution):
    '''
    Convert a grid with a center and resolution to lists
    of grid points and values at each point.
    '''
    points = get_grid_points(grid.shape, center, resolution)
    return points, grid.flatten()


def get_atom_density_kernel(shape, resolution, atom_radius, radius_mult):
    center = np.zeros(len(shape))
    points = get_grid_points(shape, center, resolution)
    density = get_atom_density(center, atom_radius, points, radius_mult)
    return density.reshape(shape)


def fit_atoms_by_GD(points, density, xyz, c, bonds, atomic_radii, max_iter, 
                    lr, mo, lambda_E=0.0, radius_multiple=1.5, verbose=0,
                    density_pred=None, density_diff=None):
    '''
    Fit atom positions, provided by arrays xyz initial positions, c channel indices, 
    and optional bonds matrix, to arrays of points with the given channel density values.
    Minimize the L2 loss (and optionally interatomic energy) between the provided density
    and fitted density by gradient descent with momentum. Return the final atom positions
    and loss.
    '''
    n_atoms = len(xyz)

    xyz = np.array(xyz)
    d_loss_d_xyz = np.zeros_like(xyz)
    d_loss_d_xyz_prev = np.zeros_like(xyz)
    
    if density_pred is None:
        density_pred = np.zeros_like(density)
    if density_diff is None:
        density_diff = np.zeros_like(density)

    ax = np.newaxis
    if lambda_E:
        xyz_diff = np.zeros((n_atoms, n_atoms, 3))
        xyz_dist = np.zeros((n_atoms, n_atoms))
        bond_length = atomic_radii[:,ax] + atomic_radii[ax,:]

    # minimize loss by gradient descent
    loss = np.inf
    i = 0
    while True:
        loss_prev = loss

        # L2 loss between predicted and true density
        density_pred[...] = 0.0
        for j in range(n_atoms):
            density_pred[:,c[j]] += get_atom_density(xyz[j], atomic_radii[j], points, radius_multiple)

        density_diff[...] = density - density_pred
        loss = (density_diff**2).sum()

        # interatomic energy of predicted atom positions
        if lambda_E:
            xyz_diff[...] = xyz[:,ax,:] - xyz[ax,:,:]
            xyz_dist[...] = np.linalg.norm(xyz_diff, axis=2)
            for j in range(n_atoms):
                loss += lambda_E * get_bond_length_energy(xyz_dist[j,j+1:], bond_length[j,j+1:], bonds[j,j+1:]).sum()

        delta_loss = loss - loss_prev
        if verbose > 2:
            print('n_atoms = {}\titer = {}\tloss = {} ({})'.format(n_atoms, i, loss, delta_loss), file=sys.stderr)

        if n_atoms == 0 or i == max_iter or abs(delta_loss)/(abs(loss_prev) + 1e-8) < 1e-2:
            break

        # compute derivatives and descend loss gradient
        d_loss_d_xyz_prev[...] = d_loss_d_xyz
        d_loss_d_xyz[...] = 0.0

        for j in range(n_atoms):
            d_density_d_xyz = get_atom_gradient(xyz[j], atomic_radii[j], points, radius_multiple)
            d_loss_d_xyz[j] += (-2*density_diff[:,c[j],ax] * d_density_d_xyz).sum(axis=0)

        if lambda_E:
            for j in range(n_atoms-1):
                d_E_d_dist = get_bond_length_gradient(xyz_dist[j,j+1], bond_length[j,j+1], bonds[j,j+1:])
                d_E_d_xyz = xyz_diff[j,j+1:] * (d_E_d_dist / xyz_dist[j,j+1:])[:,ax]
                d_xyz[j] += lambda_E * d_E_d_xyz.sum(axis=0)
                d_xyz[j+1:,:] -= lambda_E * d_E_d_xyz

        xyz[...] -= lr*(mo*d_loss_d_xyz_prev + (1-mo)*d_loss_d_xyz)
        i += 1

    return xyz, density_pred, density_diff, loss


def fit_atoms_to_grid(grid, channels, center, resolution, max_iter, lr, mo, lambda_E=0.0,
                      radius_multiple=1.5, bonded=False, max_init_bond_E=0.5, fit_channels=None,
                      verbose=0):
    '''
    Fit atoms to grid by iteratively placing atoms and then optimizing their
    positions by gradient descent on L2 loss between the provided grid density
    and the density associated with the fitted atoms.
    '''
    t_start = time.time()
    n_channels, grid_shape = grid.shape[0], grid.shape[1:]
    atomic_radii = np.array([c.atomic_radius for c in channels])
    max_n_bonds = np.array([atom_types.get_max_bonds(c.atomic_num) for c in channels])

    # convert grid to arrays of xyz points and channel density values
    points = get_grid_points(grid_shape, center, resolution)
    density = grid.reshape((n_channels, -1)).T
    density_pred = np.zeros_like(density)
    density_diff = np.zeros_like(density)

    # init atom density kernels
    kernels = [get_atom_density(center, r, points, radius_multiple).reshape(grid_shape) \
               for r in atomic_radii]

    # iteratively add atoms, fit, and assess goodness-of-fit
    xyz = np.ndarray((0, 3))
    c = np.ndarray(0, dtype=int)
    bonds = np.ndarray((0, 0))
    loss = np.inf

    while True:

        # optimize atom positions by gradient descent
        xyz, density_pred, density_diff, loss = \
            fit_atoms_by_GD(points, density, xyz, c, bonds, atomic_radii[c], max_iter, lr=lr, mo=mo,
                            lambda_E=lambda_E, radius_multiple=radius_multiple, verbose=verbose,
                            density_pred=density_pred, density_diff=density_diff)

        if verbose > 1:
            print('n_atoms = {}\t\t\tloss = {}'.format(len(xyz), loss))

        # init next atom position on remaining density
        xyz_new = []
        c_new = []
        if fit_channels is not None:
            try:
                i = fit_channels[len(c)]
                conv = conv_grid(density_diff[:,i].reshape(grid_shape), kernels[i])
                conv = np.roll(conv, np.array(grid_shape)//2, range(len(grid_shape)))
                xyz_new.append(points[conv.argmax()])
                c_new.append(i)
            except IndexError:
                pass
        else:
            c_new = []
            for i in range(n_channels):
                conv = conv_grid(density_diff[:,i].reshape(grid_shape), kernels[i])
                conv = np.roll(conv, np.array(grid_shape)//2, range(len(grid_shape)))
                if np.any(conv > (kernels[i]**2).sum()/2): # check if L2 loss decreases
                    xyz_new.append(points[conv.argmax()])
                    c_new.append(i)

        # stop if a new atom was not added
        if not xyz_new:
            break

        xyz = np.vstack([xyz, xyz_new])
        c = np.append(c, c_new)

        if bonded: # add new bonds as row and column
            raise NotImplementedError('TODO add bonds_new')
            bonds = np.vstack([bonds, bonds_new])
            bonds = np.hstack([bonds, np.append(bonds_new, 0)])

    grid_pred = density_pred.T.reshape(grid.shape)
    return xyz, c, bonds, grid_pred, loss, time.time() - t_start


def get_next_atom(points, density, xyz_init, c, atom_radius, bonded, bonds, max_n_bonds, max_init_bond_E=0.5):
    '''
    Get next atom tuple (xyz_new, c_new, bonds_new) of initial position,
    channel index, and bonds to other atoms. Select the atom as maximum
    density point within some distance range from the other atoms, given
    by positions xyz_init and channel indices c.
    '''
    xyz_new = None
    c_new = None
    bonds_new = None
    d_max = 0.0

    if bonded:
        # bond_length2[i,j] = length^2 of bond between channel[i] and channel[j]
        bond_length2 = (atom_radius[:,np.newaxis] + atom_radius[np.newaxis,:])**2
        min_bond_length2 = bond_length2 - np.log(1 + np.sqrt(max_init_bond_E))
        max_bond_length2 = bond_length2 - np.log(1 - np.sqrt(max_init_bond_E))

    # can_bond[i] = xyz_init[i] has less than its max number of bonds
    can_bond = np.sum(bonds, axis=1) < max_n_bonds[c]

    for p, d in zip(points, density):

        # more_density[i] = p has more density in channel[i] than best point so far
        more_density = d > d_max
        if np.any(more_density):

            if len(xyz_init) == 0:
                xyz_new = p
                c_new = np.argmax(d)
                bonds_new = np.array([])
                d_max = d[c_new]

            else:
                # dist2[i] = distance^2 between p and xyz_init[i]
                dist2 = np.sum((p[np.newaxis,:] - xyz_init)**2, axis=1)

                # dist_min2[i,j] = min distance^2 between p and xyz_init[i] in channel[j]
                # dist_max2[i,j] = max distance^2 between p and xyz_init[i] in channel[j]
                if bonded:
                    dist_min2 = min_bond_length2[c]
                    dist_max2 = max_bond_length2[c]
                else:
                    dist_min2 = atom_radius[c,np.newaxis]
                    dist_max2 = np.full_like(dist_min2, np.inf)

                # far_enough[i,j] = p is far enough from xyz_init[i] in channel[j]
                # near_enough[i,j] = p is near enough to xyz_init[i] in channel[j]
                far_enough = dist2[:,np.newaxis] > dist_min2
                near_enough = dist2[:,np.newaxis] < dist_max2

                # in_range[i] = p is far enough from all xyz_init and near_enough to
                # some xyz_init that can bond to make a bond in channel[i]
                in_range = np.all(far_enough, axis=0) & \
                           np.any(near_enough & can_bond[:,np.newaxis], axis=0)

                if np.any(in_range & more_density):
                    xyz_new = p
                    c_new = np.argmax(in_range*more_density*d)
                    if bonded:
                        bonds_new = near_enough[:,c_new] & can_bond
                    else:
                        bonds_new = np.zeros(len(xyz_init))
                    d_max = d[c_new]

    return xyz_new, c_new, bonds_new


def rec_and_lig_at_index_in_data_file(file, index):
    '''
    Read receptor and ligand names at a specific line number in a data file.
    '''
    with open(file, 'r') as f:
        line = f.readlines()[index]
    cols = line.rstrip().split()
    return cols[2], cols[3]


def best_loss_batch_index_from_net(net, loss_name, n_batches, best):
    '''
    Return the index of the batch that has the best loss out of
    n_batches forward passes of a net.
    '''
    loss = net.blobs[loss_name]
    best_index, best_loss = -1, None
    for i in range(n_batches):
        net.forward()
        l = float(np.max(loss.data))
        if i == 0 or best(l, best_loss) == l:
            best_loss = l
            best_index = i
            print('{} ({} / {})'.format(best_loss, i, n_batches), file=sys.stderr)
    return best_index


def n_lines_in_file(file):
    '''
    Count the number of lines in a file.
    '''
    with open(file, 'r') as f:
        return sum(1 for line in f)


def best_loss_rec_and_lig(model_file, weights_file, data_file, data_root, loss_name, best=max):
    '''
    Return the names of the receptor and ligand that have the best loss
    using a provided model, weights, and data file.
    '''
    n_batches = n_lines_in_file(data_file)
    with instantiate_model(model_file, data_file, data_file, data_root, 1) as model_file:
        net = caffe.Net(model_file, weights_file, caffe.TEST)
        index = best_loss_batch_index_from_net(net, loss_name, n_batches, best)
    return rec_and_lig_at_index_in_data_file(data_file, index)


def combine_element_grids_and_channels(grids, channels):
    '''
    Return new grids and channels by combining channels
    of provided grids that are the same element.
    '''
    element_to_idx = dict()
    new_grid = []
    new_channels = []

    for grid, channel in zip(grids, channels):

        atomic_num = channel.atomic_num
        if atomic_num not in element_to_idx:

            element_to_idx[atomic_num] = len(element_to_idx)

            new_grid.append(np.zeros_like(grid))

            name = atom_types.get_name(atomic_num)
            symbol = channel.symbol
            atomic_radius = channel.atomic_radius

            new_channel = atom_types.channel(name, atomic_num, symbol, atomic_radius)
            new_channels.append(new_channel)

        new_grid[element_to_idx[atomic_num]] += grid

    return np.array(new_grid), new_channels


def write_pymol_script(pymol_file, dx_prefixes, struct_files, centers=[]):
    '''
    Write a pymol script with a map object for each of dx_files, a
    group of all map objects (if any), a rec_file, a lig_file, and
    an optional fit_file.
    '''
    with open(pymol_file, 'w') as f:
        for dx_prefix in dx_prefixes: # load densities
            dx_pattern = '{}_*.dx'.format(dx_prefix)
            grid_name = '{}_grid'.format(os.path.basename(dx_prefix))
            f.write('load_group {}, {}\n'.format(dx_pattern, grid_name))

        for struct_file in struct_files: # load structures
            obj_name = os.path.splitext(os.path.basename(struct_file))[0]
            m = re.match(r'^(.*_fit)_(\d+)$', obj_name)
            if m:
                obj_name = m.group(1)
                state = int(m.group(2)) + 1
                f.write('load {}, {}, state={}\n'.format(struct_file, obj_name, state))
            else:
                f.write('load {}, {}\n'.format(struct_file, obj_name))

        for struct_file, (x,y,z) in zip(struct_files, centers): # center structures
            obj_name = os.path.splitext(os.path.basename(struct_file))[0]
            f.write('translate [{},{},{}], {}, camera=0\n'.format(-x, -y, -z, obj_name))


def read_gninatypes_file(lig_file, channels):

    channel_names = [c.name for c in channels]
    channel_name_idx = {n: i for i, n in enumerate(channel_names)}
    xyz, c = [], []
    with open(lig_file, 'rb') as f:
        atom_bytes = f.read(16)
        while atom_bytes:
            x, y, z, t = struct.unpack('fffi', atom_bytes)
            smina_type = atom_types.smina_types[t]
            if smina_type.name in channel_name_idx:
                c_ = channel_names.index(smina_type.name)
                xyz.append([x, y, z])
                c.append(c_)
            atom_bytes = f.read(16)
    return np.array(xyz), np.array(c)


def read_mols_from_sdf_file(sdf_file):
    '''
    Read a list of molecules from an .sdf file.
    '''
    return list(pybel.readfile('sdf', sdf_file))


def get_mol_center(mol):
    '''
    Compute the center of a molecule, ignoring hydrogen.
    '''
    return np.mean([a.coords for a in mol.atoms if a.atomicnum != 1], axis=0)


def get_n_atoms_from_sdf_file(sdf_file, idx=0):
    '''
    Count the number of atoms of each element in a molecule 
    from an .sdf file.
    '''
    mol = get_mols_from_sdf_file(sdf_file)[idx]
    return Counter(atom.GetSymbol() for atom in mol.GetAtoms())


def make_ob_mol(xyz, c, bonds, channels):
    '''
    Return an OpenBabel molecule from an array of
    xyz atom positions, channel indices, a bond matrix,
    and a list of atom type channels.
    '''
    mol = ob.OBMol()

    n_atoms = 0
    for (x, y, z), c_ in zip(xyz, c):
        atom = mol.NewAtom()
        atom.SetAtomicNum(channels[c_].atomic_num)
        atom.SetVector(x, y, z)
        n_atoms += 1

    if np.any(bonds):
        n_bonds = 0
        for i in range(n_atoms):
            atom_i = mol.GetAtom(i)
            for j in range(i+1, n_atoms):
                atom_j = mol.GetAtom(j)
                if bonds[i,j]:
                    bond = mol.NewBond()
                    bond.Set(n_bonds, atom_i, atom_j, 1, 0)
                    n_bonds += 1
    return mol


def write_ob_mols_to_sdf_file(sdf_file, mols):
    conv = ob.OBConversion()
    conv.SetOutFormat('sdf')
    for i, mol in enumerate(mols):
        conv.WriteFile(mol, sdf_file) if i == 0 else conv.Write(mol)
    conv.CloseOutFile()


def write_xyz_elems_bonds_to_sdf_file(sdf_file, xyz_elems_bonds):
    '''
    Write tuples of (xyz, elemes, bonds) atom positions and
    corresponding elements and bond matrix as chemical structures
    in an .sdf file.
    '''
    out = open(sdf_file, 'w')
    for xyz, elems, bonds in xyz_elems_bonds:
        out.write('\n mattragoza\n\n')
        n_atoms = xyz.shape[0]
        n_bonds = 0
        for i in range(n_atoms):
            for j in range(i+1, n_atoms):
                if bonds[i,j]:
                    n_bonds += 1
        out.write('{:3d}'.format(n_atoms))
        out.write('{:3d}'.format(n_bonds))
        out.write('  0  0  0  0  0  0  0  0')
        out.write('999 V2000\n')
        for (x, y, z), element in zip(xyz, elems):
            out.write('{:10.4f}'.format(x))
            out.write('{:10.4f}'.format(y))
            out.write('{:10.4f}'.format(z))
            out.write(' {:3}'.format(element))
            out.write(' 0  0  0  0  0  0  0  0  0  0  0  0\n')
        for i in range(n_atoms):
            for j in range(i+1, n_atoms):
                if bonds[i,j]:
                    out.write('{:3d}'.format(i+1))
                    out.write('{:3d}'.format(j+1))
                    out.write('  1  0  0  0\n')
        out.write('M  END\n')
        out.write('$$$$\n')
    out.close()


def write_grid_to_dx_file(dx_file, grid, center, resolution):
    '''
    Write a grid with a center and resolution to a .dx file.
    '''
    dim = grid.shape[0]
    origin = np.array(center) - resolution*(dim-1)/2.
    with open(dx_file, 'w') as f:
        f.write('object 1 class gridpositions counts %d %d %d\n' % (dim, dim, dim))
        f.write('origin %.5f %.5f %.5f\n' % tuple(origin))
        f.write('delta %.5f 0 0\n' % resolution)
        f.write('delta 0 %.5f 0\n' % resolution)
        f.write('delta 0 0 %.5f\n' % resolution)
        f.write('object 2 class gridconnections counts %d %d %d\n' % (dim, dim, dim))
        f.write('object 3 class array type double rank 0 items [ %d ] data follows\n' % (dim**3))
        total = 0
        for i in range(dim):
            for j in range(dim):
                for k in range(dim):
                    f.write('%.10f' % grid[i][j][k])
                    total += 1
                    if total % 3 == 0:
                        f.write('\n')
                    else:
                        f.write(' ')


def write_grids_to_dx_files(out_prefix, grids, channels, center, resolution):
    '''
    Write each of a list of grids a separate .dx file, using the channel names.
    '''
    dx_files = []
    for grid, channel in zip(grids, channels):
        dx_file = '{}_{}.dx'.format(out_prefix, channel.name)
        write_grid_to_dx_file(dx_file, grid, center, resolution)
        dx_files.append(dx_file)
    return dx_files


def get_sdf_file_and_idx(gninatypes_file):
    '''
    Get the name of the .sdf file and conformer idx that a
    .gninatypes file was created from.
    '''
    m = re.match(r'.*_ligand_(\d+)\.gninatypes', gninatypes_file)
    if m:
        idx = int(m.group(1))
        from_str = r'_ligand_{}\.gninatypes$'.format(idx)
        to_str = '_docked.sdf'
    else:
        idx = 0
        m = re.match(r'.*_(.+)\.gninatypes$', gninatypes_file)
        from_str = r'_{}\.gninatypes'.format(m.group(1))
        to_str = '_{}.sdf'.format(m.group(1))
    sdf_file = re.sub(from_str, to_str, gninatypes_file)
    return sdf_file, idx
        

def write_examples_to_data_file(data_file, examples):
    '''
    Write (rec_file, lig_file) examples to data_file.
    '''
    with open(data_file, 'w') as f:
        for rec_file, lig_file in examples:
            f.write('0 0 {} {}\n'.format(rec_file, lig_file))
    return data_file


def get_temp_data_file(examples):
    '''
    Write (rec_file, lig_file) examples to a temporary
    data file and return the path to the file.
    '''
    _, data_file = tempfile.mkstemp()
    write_examples_to_data_file(data_file, examples)
    return data_file


def read_examples_from_data_file(data_file, data_root=''):
    '''
    Read list of (rec_file, lig_file) examples from
    data_file, optionally prepended with data_root.
    '''
    examples = []
    with open(data_file, 'r') as f:
        for line in f:
            rec_file, lig_file = line.rstrip().split()[2:4]
            if data_root:
                rec_file = os.path.join(data_root, rec_file)
                lig_file = os.path.join(data_root, lig_file)
            examples.append((rec_file, lig_file))
    return examples


def min_RMSD(xyz1, xyz2, c):
    '''
    Compute an RMSD between two sets of positions of the same
    atom types with no prior mapping between particular atom
    positions of a given type. Returns the minimum RMSD across
    all permutations of this mapping.
    '''
    xyz1 = np.array(xyz1)
    xyz2 = np.array(xyz2)
    c = np.array(c)
    ssd = 0.0
    for c_ in sorted(set(c)):
        xyz1_c = xyz1[c == c_]
        xyz2_c = xyz2[c == c_]
        dist2_c = ((xyz1_c[:,np.newaxis,:] - xyz2_c[np.newaxis,:,:])**2).sum(axis=2)
        idx1, idx2 = sp.optimize.linear_sum_assignment(dist2_c)
        ssd += ((xyz1_c[idx1] - xyz2_c[idx2])**2).sum()
    return np.sqrt(ssd/len(c))


def find_blobs_in_net(net, blob_pattern):
    '''
    Find all blob_names in net that match blob_pattern.
    '''
    return re.findall('^{}$'.format(blob_pattern), '\n'.join(net.blobs), re.MULTILINE)


def get_layer_index(net, layer_name):
    return net._layer_names.index(layer_name)


def generate_from_model(data_net, gen_net, data_param, examples, metric_df, metric_file, pymol_file, args):
    '''
    Generate grids from a specific blob in gen_net.
    '''
    batch_size = data_param.batch_size
    resolution = data_param.resolution
    fix_center_to_origin = data_param.fix_center_to_origin
    radius_multiple = data_param.radius_multiple
    use_covalent_radius = data_param.use_covalent_radius
    channels = atom_types.get_default_lig_channels(use_covalent_radius)

    if args.prior or args.mean: # find latent variable blobs
        latent_mean = find_blobs_in_net(gen_net, r'.+_latent_mean')[0]
        latent_std = find_blobs_in_net(gen_net, r'.+_latent_std')[0]
        latent_noise = find_blobs_in_net(gen_net, r'.+_latent_noise')[0]
        latent_sample = find_blobs_in_net(gen_net, r'.+_latent_sample')[0]
        gen_net.forward() # this is necessary for proper latent sampling

    # compute metrics and write output in a separate thread
    out_queue = mp.Queue()
    out_thread = threading.Thread(
        target=out_worker_main,
        args=(out_queue, len(examples), channels, resolution, metric_df, metric_file, pymol_file, args)
    )
    out_thread.start()

    if args.fit_atoms: # fit atoms to grids in separate processes
        fit_queue = mp.Queue(args.n_fit_workers) # queue for atom fitting
        fit_pool = mp.Pool(
            processes=args.n_fit_workers,
            initializer=fit_worker_main,
            initargs=(fit_queue, out_queue)
        )

    # generate density grids from generative model in main thread
    for example_idx, (rec_file, lig_file) in enumerate(examples):

        rec_file = os.path.join(args.data_root, rec_file)
        lig_file = os.path.join(args.data_root, lig_file)

        lig_prefix, lig_ext = os.path.splitext(lig_file)
        lig_name = os.path.basename(lig_prefix)

        lig_xyz, lig_c = read_gninatypes_file(lig_prefix + '.gninatypes', channels)

        if fix_center_to_origin:
            center = np.zeros(3)
        else:
            center = np.mean(lig_xyz, axis=0)

        if args.fit_atoms: # set atom fitting parameters for the ligand
            fit_atoms = partial(fit_atoms_to_grid,
                                channels=channels,
                                center=center,
                                resolution=resolution,
                                max_iter=args.max_iter,
                                radius_multiple=radius_multiple,
                                lambda_E=args.lambda_E,
                                bonded=args.bonded,
                                verbose=args.verbose,
                                max_init_bond_E=args.max_init_bond_E,
                                fit_channels=lig_c if args.fit_atom_types else None,
                                lr=args.learning_rate,
                                mo=args.momentum)

        for sample_idx in range(args.n_samples):

            batch_idx = (example_idx*args.n_samples + sample_idx) % batch_size

            if batch_idx == 0: # forward next batch

                data_net.forward()
                gen_net.blobs['rec'].data[...] = data_net.blobs['rec'].data
                gen_net.blobs['lig'].data[...] = data_net.blobs['lig'].data

                if args.prior:
                    if args.mean:
                        gen_net.blobs[latent_mean].data[...] = 0.0
                        gen_net.blobs[latent_std].data[...] = 0.0
                        gen_net.forward(start=latent_noise)
                    else:
                        gen_net.blobs[latent_mean].data[...] = 0.0
                        gen_net.blobs[latent_std].data[...] = 1.0
                        gen_net.forward(start=latent_noise)
                else:
                    if args.mean:
                        gen_net.forward(end=latent_mean)
                        gen_net.blobs[latent_std].data[...] = 0.0
                        gen_net.forward(start=latent_noise)
                    else:
                        gen_net.forward()

            for blob_name in args.blob_name: # get grid from blob and add to appropriate queue

                grid = np.array(gen_net.blobs[blob_name].data[batch_idx])
                print('main_thread produced {} {} {}'.format(lig_name, blob_name, sample_idx))

                if args.fit_atoms:
                    fit_queue.put((lig_name, sample_idx, blob_name, center, grid, fit_atoms))
                else:
                    out_queue.put((lig_name, sample_idx, blob_name, center, grid, None, None))

    out_thread.join()


def fit_worker_main(fit_queue, out_queue):

    while True:
        print('fit_worker waiting')
        lig_name, sample_idx, grid_name, center, grid, fit_atoms = fit_queue.get()
        print('fit_worker got {} {} {}'.format(lig_name, grid_name, sample_idx))
        out_queue.put((lig_name, sample_idx, grid_name, center, grid, None, None))
        xyz, c, bonds, grid_fit, loss, t = fit_atoms(grid)
        print('fit_worker produced {} {} {}'.format(lig_name, grid_name, sample_idx))
        out_queue.put((lig_name, sample_idx, grid_name + '_fit', center, grid_fit, xyz, c))


def out_worker_main(out_queue, n_ligands, channels, resolution, metric_df, metric_file, pymol_file, args):

    n_grids_per_ligand = args.n_samples * len(args.blob_name)
    if args.fit_atoms:
        n_grids_per_ligand *= 2

    print('out_worker expects {} grids per ligand'.format(n_grids_per_ligand))
    dx_prefixes = []
    struct_files = []
    centers = []

    n_finished = 0
    all_data = defaultdict(list) # group by lig_name
    while n_finished < n_ligands:
        print('out_worker waiting')
        lig_name, sample_idx, grid_name, center, grid, xyz, c = out_queue.get()
        all_data[lig_name].append((lig_name, sample_idx, grid_name, center, grid, xyz, c))
        print('out_worker got {} {} {}'.format(lig_name, grid_name, sample_idx))

        for lig_name, lig_data in all_data.items():

            if len(lig_data) < n_grids_per_ligand: # waiting for grids
                continue

            print('out_worker unpacking/writing data for {}'.format(lig_name))

            lig_grids = defaultdict(lambda: [None for _ in range(args.n_samples)])
            lig_xyzs  = defaultdict(lambda: [None for _ in range(args.n_samples)])

            for grid_data in lig_data: # unpack and write out grid data
                lig_name, sample_idx, grid_name, center, grid, xyz, c = grid_data

                grid_prefix = '{}_{}_{}_{}'.format(args.out_prefix, lig_name, grid_name, sample_idx)
                lig_grids[grid_name][sample_idx] = grid
                if args.output_dx:
                    write_grids_to_dx_files(grid_prefix, grid, channels, center, resolution)
                    dx_prefixes.append(grid_prefix)
                
                if xyz is not None:
                    lig_xyzs[grid_name][sample_idx] = xyz

                    if args.output_sdf:
                        fit_file = '{}.sdf'.format(grid_prefix)
                        write_ob_mols_to_sdf_file(fit_file, [make_ob_mol(xyz, c, [], channels)])
                        struct_files.append(fit_file)
                        centers.append(center)

            if dx_prefixes or struct_files: # write pymol script
                write_pymol_script(pymol_file, dx_prefixes, struct_files, centers)

            print('out_worker computing metrics for {}'.format(lig_name))

            # compute generative metrics
            mean_grids = {n: np.mean(lig_grids[n], axis=0) for n in lig_grids}
            for i in range(args.n_samples):

                lig = lig_grids['lig'][i]
                lig_gen = lig_grids['lig_gen'][i]
                lig_mean = mean_grids['lig']
                lig_gen_mean = mean_grids['lig_gen']

                # density magnitude
                metric_df.loc[(lig_name, i), 'lig_norm']     = np.linalg.norm(lig)
                metric_df.loc[(lig_name, i), 'lig_gen_norm'] = np.linalg.norm(lig_gen)

                # generated density quality
                metric_df.loc[(lig_name, i), 'lig_gen_dist'] = np.linalg.norm(lig_gen - lig)

                # generated density variability
                metric_df.loc[(lig_name, i), 'lig_mean_dist'] = np.linalg.norm(lig - lig_mean)
                metric_df.loc[(lig_name, i), 'lig_gen_mean_dist'] = np.linalg.norm(lig_gen - lig_gen_mean)

                if args.fit_atom_types:

                    lig_fit = lig_grids['lig_fit'][i]
                    lig_gen_fit = lig_grids['lig_gen_fit'][i]
                    lig_fit_xyz = lig_xyzs['lig_fit'][i]
                    lig_gen_fit_xyz = lig_xyzs['lig_gen_fit'][i]

                    # fit density quality
                    metric_df.loc[(lig_name, i), 'lig_fit_dist']     = np.linalg.norm(lig_fit - lig)
                    metric_df.loc[(lig_name, i), 'lig_gen_fit_dist'] = np.linalg.norm(lig_gen_fit - lig_gen)

                    # fit structure quality
                    metric_df.loc[(lig_name, i), 'lig_gen_RMSD'] = min_RMSD(lig_gen_fit_xyz, lig_fit_xyz, c)

            #print(metric_df.loc[lig_name])

            # write out generative metrics
            metric_df.to_csv(metric_file, sep=' ')

            print('out_worker finished processing {}'.format(lig_name))

            del all_data[lig_name] # free memory
            n_finished += 1
            print('[{}/{}] finished processing {}'.format(n_finished, n_ligands, lig_name))

    print('out_worker exit')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Generate atomic density grids from generative model with Caffe')
    parser.add_argument('-d', '--data_model_file', required=True, help='prototxt file for data model')
    parser.add_argument('-g', '--gen_model_file', required=True, help='prototxt file for generative model')
    parser.add_argument('-w', '--gen_weights_file', default=None, help='.caffemodel weights for generative model')
    parser.add_argument('-r', '--rec_file', default=[], action='append', help='receptor file (relative to data_root)')
    parser.add_argument('-l', '--lig_file', default=[], action='append', help='ligand file (relative to data_root)')
    parser.add_argument('--data_file', default='', help='path to data file (generate for every example)')
    parser.add_argument('--data_root', default='', help='path to root for receptor and ligand files')
    parser.add_argument('-b', '--blob_name', default=[], action='append', help='blob(s) in model to generate from (default lig & lig_gen)')
    parser.add_argument('--n_samples', default=1, type=int, help='number of samples to generate for each input example')
    parser.add_argument('--prior', default=False, action='store_true', help='generate from prior instead of posterior distribution')
    parser.add_argument('--mean', default=False, action='store_true', help='generate mean of distribution instead of sampling')
    parser.add_argument('-o', '--out_prefix', required=True, help='common prefix for output files')
    parser.add_argument('--output_dx', action='store_true', help='output .dx files of atom density grids for each channel')
    parser.add_argument('--fit_atoms', action='store_true', help='fit atoms to density grids and print the goodness-of-fit')
    parser.add_argument('--fit_atom_types', action='store_true', help='fit exact atom types of true ligands to density grids')
    parser.add_argument('--output_sdf', action='store_true', help='output .sdf file of fit atom positions')
    parser.add_argument('--learning_rate', type=float, default=0.01, help='learning rate for atom fitting')
    parser.add_argument('--momentum', type=float, default=0.0, help='momentum for atom fitting')
    parser.add_argument('--max_iter', type=int, default=np.inf, help='maximum number of iterations for atom fitting')
    parser.add_argument('--lambda_E', type=float, default=0.0, help='interatomic bond energy loss weight for gradient descent atom fitting')
    parser.add_argument('--bonded', action='store_true', help="add atoms by creating bonds to existing atoms when atom fitting")
    parser.add_argument('--max_init_bond_E', type=float, default=0.5, help='maximum energy of bonds to consider when adding bonded atoms')
    parser.add_argument('--fit_GMM', action='store_true', help='fit atoms by a Gaussian mixture model instead of gradient descent')
    parser.add_argument('--noise_model', default='', help='noise model for GMM atom fitting (d|p)')
    parser.add_argument('-r2', '--rec_file2', default='', help='alternate receptor file (for receptor latent space)')
    parser.add_argument('-l2', '--lig_file2', default='', help='alternate ligand file (for receptor latent space)')
    parser.add_argument('--deconv_grids', action='store_true', help="apply Wiener deconvolution to atom density grids")
    parser.add_argument('--deconv_fit', action='store_true', help="apply Wiener deconvolution for atom fitting initialization")
    parser.add_argument('--noise_ratio', default=1.0, type=float, help="noise-to-signal ratio for Wiener deconvolution")
    parser.add_argument('--verbose', default=0, type=int, help="verbose output level")
    parser.add_argument('--all_iters_sdf', action='store_true', help="output a .sdf structure for each outer iteration of atom fitting")
    parser.add_argument('--gpu', action='store_true', help="generate grids from model on GPU")
    parser.add_argument('--random_rotation', default=False, action='store_true', help='randomly rotate input before generating grids')
    parser.add_argument('--random_translate', default=0.0, type=float, help='randomly translate up to #A before generating grids')
    parser.add_argument('--fix_center_to_origin', default=False, action='store_true', help='fix input grid center to origin')
    parser.add_argument('--use_covalent_radius', default=False, action='store_true', help='force input grid to use covalent radius')
    parser.add_argument('--use_default_radius', default=False, action='store_true', help='force input grid to use default radius')
    parser.add_argument('--n_fit_workers', default=mp.cpu_count(), type=int, help='number of worker processes for async atom fitting')
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    if not args.blob_name:
        args.blob_name += ['lig', 'lig_gen']

    args.fit_atoms |= args.fit_atom_types or args.output_sdf

    # read the model param files and set atom gridding params
    data_net_param = caffe_util.NetParameter.from_prototxt(args.data_model_file)
    gen_net_param = caffe_util.NetParameter.from_prototxt(args.gen_model_file)

    data_param = data_net_param.get_molgrid_data_param(caffe.TEST)
    data_param.random_rotation = args.random_rotation
    data_param.random_translate = args.random_translate
    data_param.fix_center_to_origin = args.fix_center_to_origin
    data_param.shuffle = False
    data_param.balanced = False

    assert not (args.use_covalent_radius and args.use_default_radius)
    if args.use_covalent_radius:
        data_param.use_covalent_radius = True
    elif args.use_default_radius:
        data_param.use_covalent_radius = False

    if not args.data_file: # use the set of (rec_file, lig_file) examples
        assert len(args.rec_file) == len(args.lig_file)
        examples = zip(args.rec_file, args.lig_file)

    else: # use the examples in data_file
        #assert len(args.rec_file) == len(args.lig_file) == 0
        examples = read_examples_from_data_file(args.data_file)

    data_file = get_temp_data_file(e for e in examples for i in range(args.n_samples))
    data_param.source = data_file
    data_param.root_folder = args.data_root

    # create the net in caffe
    if args.gpu:
        caffe.set_mode_gpu()
    else:
        caffe.set_mode_cpu()

    gen_net = caffe_util.Net.from_param(gen_net_param, args.gen_weights_file, phase=caffe.TEST)

    data_param.batch_size = gen_net.blobs['lig'].shape[0]
    data_net = caffe_util.Net.from_param(data_net_param, phase=caffe.TEST)

    columns = ['lig_name', 'sample_idx']
    metric_df = pd.DataFrame(columns=columns).set_index(columns)
    metric_file = '{}.gen_metrics'.format(args.out_prefix)
    pymol_file = '{}.pymol'.format(args.out_prefix)

    generate_from_model(data_net, gen_net, data_param, examples, metric_df, metric_file, pymol_file, args)


if __name__ == '__main__':
    main(sys.argv[1:])
