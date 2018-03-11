#!/usr/bin/env python2.7
# Copyright (C) 2015-2018 Daniel Asarnow, Eugene Palovcak
# University of California, San Francisco
#
# Program for projection subtraction in electron microscopy.
# See help text and README file for more information.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import print_function
import logging
import numba
import numpy as np
import os.path
import pandas as pd
import pyfftw
import Queue
import sys
import threading
from multiprocessing.dummy import Pool
from numpy.fft import fftshift
from pyem import mrc
from pyem.algo import bincorr_nb
from pyem.ctf import eval_ctf
from pyem.star import calculate_apix
from pyem.star import parse_star
from pyem.star import write_star
from pyem.util import euler2rot
from pyem.vop import interpolate_slice_numba
from pyem.vop import vol_ft
from pyfftw.interfaces.numpy_fft import rfft2
from pyfftw.interfaces.numpy_fft import irfft2


def main(args):
    """
    Projection subtraction program entry point.
    :param args: Command-line arguments parsed by ArgumentParser.parse_args()
    :return: Exit status
    """

    log = logging.getLogger('root')
    hdlr = logging.StreamHandler(sys.stdout)
    log.addHandler(hdlr)
    log.setLevel(logging.getLevelName(args.loglevel.upper()))

    # rchop = lambda x, y: x if not x.endswith(y) or len(y) == 0 else x[:-len(y)]
    # args.output = rchop(args.output, ".star")
    # args.suffix = rchop(args.suffix, ".mrc")
    # args.suffix = rchop(args.suffix, ".mrcs")

    log.debug("Reading particle .star file")
    df = parse_star(args.input, keep_index=False)
    df.reset_index(inplace=True)
    df["rlnOriginalImageName"] = df["rlnImageName"]
    df["ucsfOriginalParticleIndex"], df["ucsfOriginalImagePath"] = df["rlnOriginalImageName"].str.split("@").str
    df["ucsfOriginalParticleIndex"] = pd.to_numeric(df["ucsfOriginalParticleIndex"])
    df.sort_values("rlnOriginalImageName", inplace=True, kind="mergesort")
    gb = df.groupby("ucsfOriginalImagePath")
    df["ucsfParticleIndex"] = gb.cumcount()
    df["ucsfImagePath"] = df["ucsfOriginalImagePath"].map(
        lambda x: os.path.join(args.dest,
                               args.prefix + os.path.basename(x).replace(".mrcs", args.suffix + ".mrcs")))
    df["rlnImageName"] = df["ucsfParticleIndex"].map(
        lambda x: "%.6d" % x).str.cat(df["ucsfImagePath"], sep="@")
    log.debug("Read particle .star file")

    if args.submap_ft is None:
        submap = mrc.read(args.submap, inc_header=False, compat="relion")
        submap_ft = vol_ft(submap, threads=4)
    else:
        log.debug("Loading %s" % args.submap_ft)
        submap_ft = np.load(args.submap_ft)
        log.debug("Loaded %s" % args.submap_ft)

    sz = submap_ft.shape[0] // 2 - 1
    sx, sy = np.meshgrid(np.fft.rfftfreq(sz), np.fft.fftfreq(sz))
    s = np.sqrt(sx**2 + sy**2)
    r = s * sz
    r = np.round(r).astype(np.int64)
    r[r > sz // 2] = sz // 2 + 1
    nr = np.max(r) + 1
    a = np.arctan2(sy, sx)

    if args.refmap is not None:
        coefs_method = 1
        if args.refmap_ft is None:
            refmap = mrc.read(args.refmap, inc_header=False, compat="relion")
            refmap_ft = vol_ft(refmap, threads=4)
        else:
            log.debug("Loading %s" % args.refmap_ft)
            refmap_ft = np.load(args.refmap_ft)
            log.debug("Loaded %s" % args.refmap_ft)
    else:
        coefs_method = 0
        refmap_ft = np.empty(submap_ft.shape, dtype=submap_ft.dtype)
    apix = calculate_apix(df)

    log.debug("Constructing particle metadata references")
    # npart = df.shape[0]
    idx = df["ucsfOriginalParticleIndex"].values
    stack = df["ucsfOriginalImagePath"].values.astype(np.str, copy=False)
    def1 = df["rlnDefocusU"].values
    def2 = df["rlnDefocusV"].values
    angast = df["rlnDefocusAngle"].values
    phase = df["rlnPhaseShift"].values
    kv = df["rlnVoltage"].values
    ac = df["rlnAmplitudeContrast"].values
    cs = df["rlnSphericalAberration"].values
    az = df["rlnAngleRot"].values
    el = df["rlnAngleTilt"].values
    sk = df["rlnAnglePsi"].values
    xshift = df["rlnOriginX"].values
    yshift = df["rlnOriginY"].values
    new_idx = df["ucsfParticleIndex"].values
    new_stack = df["ucsfImagePath"].values.astype(np.str, copy=False)

    log.debug("Grouping particles by output stack")
    gb = df.groupby("ucsfImagePath")

    qsize = 1000
    fftthreads=1
    pyfftw.interfaces.cache.enable()

    log.debug("Instantiating worker pool")
    pool = Pool(processes=args.nproc)

    for fname, particles in gb.indices.iteritems():
        log.debug("Instantiating queue")
        queue = Queue.Queue(maxsize=qsize)
        log.debug("Start consumer for %s" % fname)
        thread = threading.Thread(target=consumer, args=(queue, fname, apix, fftthreads))
        thread.start()
        log.debug("Calling producer()")
        producer(pool, queue, submap_ft, refmap_ft, particles, idx, stack,
                  sx, sy, s, a, apix, def1, def2, angast, phase, kv, ac, cs,
                  az, el, sk, xshift, yshift,
                  new_idx, new_stack, coefs_method, r, nr, fftthreads=fftthreads)
        log.debug("Producer returned for %s" % fname)
        thread.join()
        log.debug("Done waiting for consumer to return")

    pool.close()
    pool.join()
    pool.terminate()

    df.drop([c for c in df.columns if "ucsf" in c or "eman" in c], axis=1, inplace=True)

    df.set_index("index", inplace=True)
    df.sort_index(inplace=True, kind="mergesort")

    write_star(args.output, df, reindex=True)

    return 0


@numba.jit(cache=True, nopython=True, nogil=True)
def subtract(p1, submap_ft, refmap_ft,
             sx, sy, s, a, apix, def1, def2, angast, phase, kv, ac, cs,
             az, el, sk, xshift, yshift, coefs_method, r, nr):
    c = eval_ctf(s / apix, a, def1, def2, angast, phase, kv, ac, cs, bf=0, lp=2 * apix)

    orient = euler2rot(np.deg2rad(az), np.deg2rad(el), np.deg2rad(sk))
    pshift = np.exp(-2 * np.pi * 1j * (-xshift * sx + -yshift * sy))
    p2 = interpolate_slice_numba(submap_ft, orient)
    p2 *= pshift

    if coefs_method < 1:
        p1s = p1 - p2 * c
    elif coefs_method == 1:
        p3 = interpolate_slice_numba(refmap_ft, orient)
        p3 *= pshift
        frc = np.abs(bincorr_nb(p1, p3 * c, r, nr))
        coefs = np.take(frc, r)
        p1s = p1 - p2 * c * coefs

    return p1s


def producer(pool, queue, submap_ft, refmap_ft, particles, idx, stack,
                  sx, sy, s, a, apix, def1, def2, angast, phase, kv, ac, cs,
                  az, el, sk, xshift, yshift,
                  new_idx, new_stack, coefs_method, r, nr, fftthreads=1):
    log = logging.getLogger('root')
    for i in particles:
        log.debug("Producing %d@%s" % (idx[i], stack[i]))
        p1r = mrc.read_imgs(stack[i], idx[i] - 1, compat="relion")
        p1 = rfft2(fftshift(p1r), threads=fftthreads)
        log.debug("Apply")
        ri = pool.apply_async(subtract,
                  (p1, submap_ft, refmap_ft,
                   sx, sy, s, a, apix,
                   def1[i], def2[i], angast[i],
                   phase[i], kv[i], ac[i], cs[i],
                   az[i], el[i], sk[i], xshift[i], yshift[i],
                   coefs_method, r, nr))
        log.debug("Put")
        queue.put((new_idx[i], ri), block=True)

    # Either the poison-pill-put blocks, we have multiple queues and
    # consumers, or the consumer knows maps results to multiple files.
    log.debug("Put poison pill")
    queue.put((-1, None), block=True)


def consumer(queue, stack, apix=1.0, fftthreads=1):
    log = logging.getLogger('root')
    while True:
        log.debug("Get")
        i, ri = queue.get(block=True)
        log.debug("Got %d" % i)
        if i == -1:
            break
        p1s = ri.get()
        log.debug("Result for %d was shape (%d,%d)" % (i, p1s.shape[0], p1s.shape[1]))
        new_image = irfft2(fftshift(p1s, axes=0), threads=fftthreads)
        if i == 0:
            log.debug("Write %d@%s" % (i, stack))
            mrc.write(stack, new_image, psz=apix)
        else:
            log.debug("Append %d@%s" % (i, stack))
            mrc.append(stack, new_image)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(version="projection_subtraction.py 2.0a")
    parser.add_argument("input", type=str, help="STAR file with original particles")
    parser.add_argument("output", type=str, help="STAR file with subtracted particles)")
    parser.add_argument("--dest", type=str, help="Destination directory for subtracted particle stacks")
    parser.add_argument("--refmap", type=str, help="Map used to calculate reference projections")
    parser.add_argument("--submap", type=str, help="Map used to calculate subtracted projections")
    parser.add_argument("--refmap_ft", type=str,
                        help="Fourier transform used to calculate reference projections (.npy)")
    parser.add_argument("--submap_ft", type=str,
                        help="Fourier transform used to calculate subtracted projections (.npy)")
    parser.add_argument("--nproc", type=int, default=None, help="Number of parallel processes")
    # parser.add_argument("--maxchunk", type=int, default=1000, help="Maximum task chunk size")
    parser.add_argument("--loglevel", type=str, default="WARNING", help="Logging level and debug output")
    # parser.add_argument("--recenter", action="store_true", default=False,
    #                     help="Shift particle origin to new center of mass")
    # parser.add_argument("--low-cutoff", type=float, default=0.0, help="Low cutoff frequency")
    # parser.add_argument("--high-cutoff", type=float, default=0.7071, help="High cutoff frequency")
    parser.add_argument("--prefix", type=str, help="Additional prefix for particle stacks", default="")
    parser.add_argument("--suffix", type=str, help="Additional suffix for particle stacks")

    sys.exit(main(parser.parse_args()))
