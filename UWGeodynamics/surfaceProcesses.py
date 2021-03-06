from __future__ import print_function,  absolute_import
import abc
import underworld.function as fn
import numpy as np
import sys
from scipy.ndimage.filters import gaussian_filter
from scipy.interpolate import griddata, interp1d
from UWGeodynamics import non_dimensionalise as nd
from UWGeodynamics import dimensionalise
from UWGeodynamics import UnitRegistry as u
from mpi4py import MPI as _MPI
from tempfile import gettempdir

from scipy.linalg import solve as linSolve
import scipy.signal as sig

comm = _MPI.COMM_WORLD
rank = comm.rank
size = comm.size

ABC = abc.ABCMeta('ABC', (object,), {})
_tempdir = gettempdir()


class SurfaceProcesses(ABC):

    def __init__(self, Model=None):

        self.Model = Model

    @property
    def Model(self):
        return self._Model

    @Model.setter
    def Model(self, value):
        self._Model = value
        if value:
            self._init_model()

    @abc.abstractmethod
    def _init_model(self):
        pass

    @abc.abstractmethod
    def solve(self, dt):
        pass


class Badlands(SurfaceProcesses):
    """ A wrapper class for Badlands """

    def __init__(self, airIndex,
                 sedimentIndex, XML, resolution, checkpoint_interval,
                 surfElevation=0., verbose=True, Model=None, outputDir="outbdls",
                 restartFolder=None, restartStep=None, timeField=None,
                 minCoord=None, maxCoord=None, aspectRatio2d=1.):
        try:
            import pyBadlands

        except ImportError:
            raise ImportError("""pyBadlands import as failed. Please check your
                              installation, PYTHONPATH and PATH environment
                              variables""")

        self.verbose = verbose
        self.outputDir = outputDir
        self.restartStep = restartStep
        self.restartFolder = restartFolder

        self.airIndex = airIndex
        self.sedimentIndex = sedimentIndex
        self.resolution = nd(resolution)
        self.surfElevation = fn.Function.convert(nd(surfElevation))
        self.checkpoint_interval = nd(checkpoint_interval)
        self.timeField = timeField
        self.XML = XML
        self.time_years = 0.
        self.minCoord = minCoord
        self.maxCoord = maxCoord
        self.aspectRatio2d = aspectRatio2d
        self.Model = Model

    def _init_model(self):

        if self.minCoord:
            self.minCoord = tuple([nd(val) for val in self.minCoord])
        else:
            self.minCoord = self.Model.mesh.minCoord

        if self.maxCoord:
            self.maxCoord = tuple([nd(val) for val in self.maxCoord])
        else:
            self.maxCoord = self.Model.mesh.maxCoord

        if self.Model.mesh.dim == 2:
            self.minCoord = (self.minCoord[0], self.aspectRatio2d*self.minCoord[0])
            self.maxCoord = (self.maxCoord[0], self.aspectRatio2d*self.maxCoord[0])

        if rank == 0:
            from pyBadlands.model import Model as BadlandsModel
            self.badlands_model = BadlandsModel()
            self.badlands_model.load_xml(self.XML)

            if self.restartStep:
                self.badlands_model.input.restart = True
                self.badlands_model.input.rstep = self.restartStep
                self.badlands_model.input.rfolder = self.restartFolder
                self.badlands_model.input.outDir = self.restartFolder
                self.badlands_model.outputStep = self.restartStep

                # Parse xmf for the last timestep time
                import xml.etree.ElementTree as etree
                xmf = (self.restartFolder +
                       "/xmf/tin.time" +
                       str(self.restartStep) + ".xmf")
                tree = etree.parse(xmf)
                root = tree.getroot()
                self.time_years = float(root[0][0][0].attrib["Value"])

            # Create Initial DEM
            self._demfile = _tempdir + "/dem.csv"
            self.dem = self._generate_dem()
            np.savetxt(self._demfile, self.dem)

            # Build Mesh
            self.badlands_model.build_mesh(self._demfile, verbose=False)

            self.badlands_model.input.outDir = self.outputDir
            self.badlands_model.input.disp3d = True  # enable 3D displacements
            self.badlands_model.input.region = 0  # TODO: check what this does
            self.badlands_model.input.tStart = self.time_years
            self.badlands_model.tNow = self.time_years

            # Override the checkpoint/display interval in the Badlands model to
            # ensure BL and UW are synced
            self.badlands_model.input.tDisplay = (
            dimensionalise(self.checkpoint_interval, u.years).magnitude)

            # Set Badlands minimal distance between nodes before regridding
            self.badlands_model.force.merge3d = (
                self.badlands_model.input.Afactor *
                self.badlands_model.recGrid.resEdges * 0.5)

            # Bodge Badlands to perform an initial checkpoint
            # FIXME: we need to run the model for at least one
            # iteration before this is generated.
            # It would be nice if this wasn't the case.
            self.badlands_model.force.next_display = 0

        comm.Barrier()

        self._disp_inserted = False

        # Transfer the initial DEM state to Underworld
        self._update_material_types()
        comm.Barrier()

    def _generate_dem(self):
        """
        Generate a badlands DEM. This can be used as the initial Badlands state.

        """

        # Calculate number of nodes from required resolution.
        nx = np.int((self.maxCoord[0] - self.minCoord[0]) / self.resolution)
        ny = np.int((self.maxCoord[1] - self.minCoord[1]) / self.resolution)
        nx += 1
        ny += 1

        x = np.linspace(self.minCoord[0], self.maxCoord[0], nx)
        y = np.linspace(self.minCoord[1], self.maxCoord[1], ny)

        coordsX, coordsY = np.meshgrid(x, y)

        dem = np.zeros((nx * ny, 3))
        dem[:, 0] = coordsX.flatten()
        dem[:, 1] = coordsY.flatten()

        coordsZ = self.surfElevation.evaluate(dem[:, :2])

        dem[:, 2] = coordsZ.flatten()
        return dimensionalise(dem, u.meter).magnitude

    def solve(self, dt, sigma=0):
        if rank == 0 and self.verbose:
            purple = "\033[0;35m"
            endcol = "\033[00m"
            print(purple + "Processing surface with Badlands" + endcol)
            sys.stdout.flush()

        np_surface = None
        if rank == 0:
            rg = self.badlands_model.recGrid
            if self.Model.mesh.dim == 2:
                zVals = rg.regZ.mean(axis=1)
                np_surface = np.column_stack((rg.regX, zVals))

            if self.Model.mesh.dim == 3:
                np_surface = np.column_stack((rg.rectX, rg.rectY, rg.rectZ))

        np_surface = comm.bcast(np_surface, root=0)
        comm.Barrier()

        # Get Velocity Field at the surface
        nd_coords = nd(np_surface * u.meter)
        tracer_velocity = self.Model.velocityField.evaluate_global(nd_coords)

        dt_years = dimensionalise(dt, u.years).magnitude

        if rank == 0:
            tracer_disp = dimensionalise(tracer_velocity * dt, u.meter).magnitude
            self._inject_badlands_displacement(self.time_years,
                                               dt_years,
                                               tracer_disp, sigma)

            # Run the Badlands model to the same time point
            self.badlands_model.run_to_time(self.time_years + dt_years)

        self.time_years += dt_years

        # TODO: Improve the performance of this function
        self._update_material_types()
        comm.Barrier()

        if rank == 0 and self.verbose:
            purple = "\033[0;35m"
            endcol = "\033[00m"
            print(purple + "Processing surface with Badlands...Done" + endcol)
            sys.stdout.flush()

        return

    def _determine_particle_state_2D(self):

        known_xy = None
        known_z = None
        xs = None
        ys = None
        fact = dimensionalise(1.0, u.meter).magnitude
        if rank == 0:
            # points that we have known elevation for
            known_xy = self.badlands_model.recGrid.tinMesh['vertices'] / fact
            # elevation for those points
            known_z = self.badlands_model.elevation / fact
            xs = self.badlands_model.recGrid.regX / fact
            ys = self.badlands_model.recGrid.regY / fact

        known_xy = comm.bcast(known_xy, root=0)
        known_z = comm.bcast(known_z, root=0)
        xs = comm.bcast(xs, root=0)
        ys = comm.bcast(ys, root=0)

        comm.Barrier()

        grid_x, grid_y = np.meshgrid(xs, ys)
        interpolate_z = griddata(known_xy,
                                 known_z,
                                 (grid_x, grid_y),
                                 method='nearest').T
        interpolate_z = interpolate_z.mean(axis=1)

        f = interp1d(xs, interpolate_z)

        uw_surface = self.Model.swarm.particleCoordinates.data
        bdl_surface = f(uw_surface[:, 0])

        flags = uw_surface[:, 1] < bdl_surface

        return flags

    def _determine_particle_state(self):
        # Given Badlands' mesh, determine if each particle in 'volume' is above
        # (False) or below (True) it.

        # To do this, for each X/Y pair in 'volume', we interpolate its Z value
        # relative to the mesh in blModel. Then, if the interpolated Z is
        # greater than the supplied Z (i.e. Badlands mesh is above particle
        # elevation) it's sediment (True). Else, it's air (False).

        # TODO: we only support air/sediment layers right now; erodibility
        # layers are not implemented

        known_xy = None
        known_z = None
        fact = dimensionalise(1.0, u.meter).magnitude
        if rank == 0:
            # points that we have known elevation for
            known_xy = self.badlands_model.recGrid.tinMesh['vertices'] / fact
            known_z = self.badlands_model.elevation / fact

        known_xy = comm.bcast(known_xy, root=0)
        known_z = comm.bcast(known_z, root=0)

        comm.Barrier()

        volume = self.Model.swarm.particleCoordinates.data

        interpolate_xy = volume[:, [0, 1]]

        # NOTE: we're using nearest neighbour interpolation. This should be
        # sufficient as Badlands will normally run at a much higher resolution
        # than Underworld. 'linear' interpolation is much, much slower.
        interpolate_z = griddata(points=known_xy,
                                 values=known_z,
                                 xi=interpolate_xy,
                                 method='nearest')

        # True for sediment, False for air
        flags = volume[:, 2] < interpolate_z

        return flags

    def _update_material_types(self):

        # What do the materials (in air/sediment terms) look like now?
        if self.Model.mesh.dim == 3:
            material_flags = self._determine_particle_state()
        if self.Model.mesh.dim == 2:
            material_flags = self._determine_particle_state_2D()

        # If any materials changed state, update the Underworld material types
        mi = self.Model.materialField.data

        # convert air to sediment
        for air_material in self.airIndex:
            sedimented_mask = np.logical_and(np.in1d(mi, air_material), material_flags)
            mi[sedimented_mask] = self.sedimentIndex

        # convert sediment to air
        for air_material in self.airIndex:
            eroded_mask = np.logical_and(~np.in1d(mi, air_material), ~material_flags)
            mi[eroded_mask] = self.airIndex[0]

    def _inject_badlands_displacement(self, time, dt, disp, sigma):
        """
        Takes a plane of tracer points and their DISPLACEMENTS in 3D over time
        period dt applies a gaussian filter on it. Injects it into Badlands as 3D
        tectonic movement.
        """

        # The Badlands 3D interpolation map is the displacement of each DEM
        # node at the end of the time period relative to its starting position.
        # If you start a new displacement file, it is treated as starting at
        # the DEM starting points (and interpolated onto the TIN as it was at
        # that tNow).

        # kludge; don't keep adding new entries
        if self._disp_inserted:
            self.badlands_model.force.T_disp[0, 0] = time
            self.badlands_model.force.T_disp[0, 1] = (time + dt)
        else:
            self.badlands_model.force.T_disp = np.vstack(([time, time + dt], self.badlands_model.force.T_disp))
            self._disp_inserted = True

        # Extent the velocity field in the third dimension
        if self.Model.mesh.dim == 2:
            dispX = np.tile(disp[:, 0], self.badlands_model.recGrid.rny)
            dispY = np.zeros((self.badlands_model.recGrid.rnx * self.badlands_model.recGrid.rny,))
            dispZ = np.tile(disp[:, 1], self.badlands_model.recGrid.rny)

            disp = np.zeros((self.badlands_model.recGrid.rnx * self.badlands_model.recGrid.rny,3))
            disp[:, 0] = dispX
            disp[:, 1] = dispY
            disp[:, 2] = dispZ

        # Gaussian smoothing
        if sigma > 0:
            dispX = np.copy(disp[:, 0]).reshape(self.badlands_model.recGrid.rnx, self.badlands_model.recGrid.rny)
            dispY = np.copy(disp[:, 1]).reshape(self.badlands_model.recGrid.rnx, self.badlands_model.recGrid.rny)
            dispZ = np.copy(disp[:, 2]).reshape(self.badlands_model.recGrid.rnx, self.badlands_model.recGrid.rny)
            smoothX = gaussian_filter(dispX, sigma)
            smoothY = gaussian_filter(dispY, sigma)
            smoothZ = gaussian_filter(dispZ, sigma)
            disp[:, 0] = smoothX.flatten()
            disp[:, 1] = smoothY.flatten()
            disp[:, 2] = smoothZ.flatten()

        self.badlands_model.force.injected_disps = disp


class ErosionThreshold(SurfaceProcesses):

    def __init__(self, air=None, threshold=None, surfaceTracers=None,
                 Model=None, **kwargs):

        super(ErosionThreshold, self).__init__(Model=Model)

        self.Model = Model
        self.threshold = threshold
        self.air = air
        self.surfaceTracers = surfaceTracers
        self.Model = Model

    def _init_model(self):

        materialField = self.Model.materialField

        materialMap = {}
        for material in self.air:
            materialMap[material.index] = 1.0

        isAirMaterial = fn.branching.map(fn_key=materialField,
                                         mapping=materialMap,
                                         fn_default=0.0)

        belowthreshold = [(((isAirMaterial < 0.5) & (fn.input()[1] > nd(self.threshold))), self.air[0].index),
                          (True, materialField)]

        self._fn = fn.branching.conditional(belowthreshold)

    def solve(self, dt):

        if not self.Model:
            raise ValueError("Model is not defined")

        self.Model.materialField.data[:] = self._fn.evaluate(self.Model.swarm)
        if self.surfaceTracers:
            if self.surfaceTracers.swarm.particleCoordinates.data.size > 0:
                coords = self.surfaceTracers.swarm.particleCoordinates
                coords.data[coords.data[:, -1] > nd(self.threshold), -1] = nd(self.threshold)
        return


class SedimentationThreshold(SurfaceProcesses):

    def __init__(self, air=None, sediment=None,
                 threshold=None, timeField=None, Model=None,
                 surfaceTracers=None, **kwargs):

        super(SedimentationThreshold, self).__init__(Model=Model)

        self.timeField = timeField
        self.air = air
        self.sediment = sediment
        self.threshold = threshold
        self.surfaceTracers = surfaceTracers
        self.Model = Model

    def _init_model(self):

        materialField = self.Model.materialField

        materialMap = {}
        for material in self.air:
            materialMap[material.index] = 1.0

        isAirMaterial = fn.branching.map(fn_key=materialField,
                                         mapping=materialMap,
                                         fn_default=0.0)

        belowthreshold = [(((isAirMaterial > 0.5) & (fn.input()[1] < nd(self.threshold))), 0.),
                          (True, 1.)]

        self._change_material = fn.branching.conditional(belowthreshold)

        conditions = [(self._change_material < 0.5, self.sediment[0].index),
                      (True, materialField)]

        self._fn = fn.branching.conditional(conditions)

    def solve(self, dt):

        if not self.Model:
            raise ValueError("Model is not defined")

        if self.timeField:
            fn = self._change_material * self.timeField
            self.timeField.data[...] = fn.evaluate(self.Model.swarm)

        self.Model.materialField.data[:] = self._fn.evaluate(self.Model.swarm)

        if self.surfaceTracers:
            if self.surfaceTracers.swarm.particleCoordinates.data.size > 0:
                coords = self.surfaceTracers.swarm.particleCoordinates
                coords.data[coords.data[:, -1] < nd(self.threshold), -1] = nd(self.threshold)


class ErosionAndSedimentationThreshold(SedimentationThreshold, ErosionThreshold):

    def __init__(self, air=None, sediment=None,
                 threshold=None, timeField=None,
                 surfaceTracers=None, Model=None, **kwargs):

        super(ErosionAndSedimentationThreshold, self).__init__(Model=Model)

        self.timeField = timeField
        self.air = air
        self.sediment = sediment
        self.threshold = threshold
        self.surfaceTracers = surfaceTracers
        self.Model = Model

    def _init_model(self):

        ErosionThreshold._init_model(self)
        SedimentationThreshold._init_model(self)

    def solve(self, dt):

        ErosionThreshold.solve(self, dt)
        SedimentationThreshold.solve(self, dt)

class BasicHillSlopeDiffsuion2d(object):
    def __init__(self,
                 Model=None,
                 airIndex=None,
                 sedimentIndex=None,
                 diffusivity=None,
                 interfaceHeight=0.,
                 timeField=None,
                 filterTopo=False,
                 verbose=True):

        # Create references to Model variables
        self.materialField = Model.materialField
        self.airIndex = airIndex
        self.sedimentIndex = sedimentIndex
        self.Ks = diffusivity
        self.mesh = Model.mesh
        self.velocityField = Model.velocityField
        self.swarm = Model.swarm
        self.minX = nd(Model.minCoord[0])
        self.maxX = nd(Model.maxCoord[0])

        # Define the number of topographic markers, 4 times the Model mesh resolution
        self.topoNum = 4 * Model.elementRes[0] + 1
        self.topostp = (self.maxX - self.minX) / (self.topoNum - 1)

        # initiate the 1d FCT Grid for topographic diffusion
        self.gridt = np.zeros((6, self.topoNum))
        self.gridt[0, :] = np.linspace(self.minX, self.maxX, self.topoNum)
        self.gridt[1, :] = nd(interfaceHeight)
        self.filterTopo = filterTopo
        self.verbose = verbose

    def SurfaceVeloEval(self, mesh=None, velocityField=None):

        minX = self.minX
        maxX = self.maxX

        self.gridt[3:6, :] = 0.0

        tmp = np.where(
            (self.gridt[0, :] >= minX) & (self.gridt[0, :] <= maxX) &
            (self.gridt[0, :] >= mesh.data[0:mesh.nodesLocal, 0].min()) &
            (self.gridt[0, :] <= mesh.data[0:mesh.nodesLocal, 0].max())
            & (self.gridt[1, :] <= mesh.data[0:mesh.nodesLocal, 1].max()))[0]

        if len(tmp) > 0:
            tmp2 = velocityField.evaluate(np.squeeze(self.gridt[0:2, tmp]).T)
            self.gridt[3, tmp] = tmp2.T[0, :]
            self.gridt[4, tmp] = tmp2.T[1, :]

            tmp = np.where(
                (self.gridt[0, :] > minX) & (self.gridt[0, :] < maxX) &
                ((self.gridt[0, :] == mesh.data[0:mesh.nodesLocal, 0].min())
                 | (self.gridt[0, :] == mesh.data[0:mesh.nodesLocal, 0].max()))
            )[0]
            # boundary between two cpus, there velocity is reduced
            if len(tmp) > 0:
                # import ipdb; ipdb.set_trace()
                print ('hgn', tmp, self.gridt[0:2, tmp],np.squeeze(self.gridt[0:2, tmp]).T)
                if len(tmp) == 1:
                    tmp2 = velocityField.evaluate((self.gridt[0, tmp][0],
                                                   self.gridt[1, tmp][0]))
                else:
                    tmp2 = velocityField.evaluate(
                        np.squeeze(self.gridt[0:2, tmp]).T)
                self.gridt[3, tmp] = tmp2.T[0, :] / 2.
                self.gridt[4, tmp] = tmp2.T[1, :] / 2.

    def SurfaceProcess(self, dt):

        Ks = self.Ks
        topoNum = self.topoNum
        topostp = self.topostp
        minX = self.minX
        maxX = self.maxX
        # refer to Collision.m in Chapter_17 of Gerya_numerical_geodynamics book
        # first advect topography vertically
        # and diffuse topography (downhill diffusion)
        L = np.zeros((topoNum, topoNum))
        R = np.zeros((topoNum, 1))
        # first point: symmetry
        L[0, 0] = 1.
        L[0, 1] = -1.
        R[0] = 0.0
        # from IPython.core.debugger import Tracer; Tracer()()
        # Intermediate Points
        K2 = Ks * dt / topostp**2
        for i1 in range(1, topoNum - 1):
            # Internal points
            if (self.gridt[0, i1] >= minX and self.gridt[0, i1] <= maxX):
                L[i1, i1 - 1] = -K2
                L[i1, i1] = 1 + 2 * K2
                L[i1, i1 + 1] = -K2
                R[i1] = self.gridt[1, i1] + self.gridt[4, i1] * dt
            else:
                # left of the left boundary
                if (self.gridt[0, i1] < minX):
                    L[i1, i1] = 1.
                    L[i1, i1 + 1] = -1.
                    R[i1] = 0

                # right of the right boundary
                if (self.gridt[0, i1] > maxX):
                    L[i1, i1] = 1.
                    L[i1, i1 - 1] = -1.
                    R[i1] = 0

        # last point: symmetry
        L[topoNum - 1, topoNum - 1] = 1.
        L[topoNum - 1, topoNum - 2] = -1.
        R[topoNum - 1] = 0.

        # solve matrix
        self.gridt[1, :] = np.squeeze(linSolve(L, R))
        # Second, advect topography horizontally
        vxmax = max(np.abs(self.gridt[
            3, :]))  # maximum horizontal velocity at topography nodes
        # defining topography advection timestep
        ntSP = 1
        dtSP = dt
        if vxmax > 0:
            dtSP = min(topostp / vxmax, dt)
            if dtSP < dt:
                ntSP = np.ceil(dt / dtSP)
                dtSP = dt / ntSP

        # define FCT parameter MU
        mu = 1.0 / 8
        # advect topography with FCT
        for i1 in range(ntSP):
            # step 0: set new profile
            self.gridt[2, :] = self.gridt[1, :]
            # step 1: Transport + numerical diffusion stage
            for i2 in range(1, topoNum - 1):
                # define FCT parameters EPS and NU
                eps = self.gridt[3, i2] * dtSP / topostp
                nu = 1. / 8 + eps**2 / 2.
                # change topo
                self.gridt[2, i2] = self.gridt[1, i2] - eps / 2 * (
                    self.gridt[1, i2 + 1] - self.gridt[1, i2 - 1]) + nu * (
                        self.gridt[1, i2 + 1] - 2 * self.gridt[1, i2] +
                        self.gridt[1, i2 - 1])

            # step 2: anti-difussion stage
            # anti-diffusion flow for the first cell
            self.gridt[5, 0] = 0
            for i2 in range(1, topoNum - 2):
                # corrected antidiffusion flow for current cell
                delt0 = self.gridt[2, i2] - self.gridt[2, i2 - 1]
                delt1 = self.gridt[2, i2 + 1] - self.gridt[2, i2]
                delt2 = self.gridt[2, i2 + 2] - self.gridt[2, i2 + 1]
                s = np.copysign(1.0, delt1)
                self.gridt[5, i2] = s * max(
                    0.0, min(min(s * delt2, s * delt0), mu * abs(delt1)))
                self.gridt[
                    1,
                    i2] = self.gridt[2, i2] - self.gridt[5,
                                                         i2] + self.gridt[5, i2
                                                                          - 1]

        # Filter/Moving average to remove smale scale instabilities
        # for certain values of Ks or when dt is large

    #
        if self.filterTopo:
            self.gridt[1, :] = sig.savgol_filter(
                self.gridt[1, :], 3, 1, mode='nearest')
        return

    def ErosionAndSedimentation(self):

        airIndex = self.airIndex
        sedimentIndex = self.sedimentIndex

        # generate an interpolation function, nearest seems to be the fastest option, refer to linkage module.
        surface_function = interp1d(
            self.gridt[0, :], self.gridt[1, :], kind='nearest')
        swarm_coords = self.swarm.particleCoordinates.data
        surface_ycoord = surface_function(swarm_coords[:, 0])
        material_flags = swarm_coords[:, 1] < surface_ycoord

        # convert air to sediment
        sedimented_mask = np.logical_and(
            np.in1d(self.materialField.data, airIndex), material_flags)
        self.materialField.data[sedimented_mask] = sedimentIndex

        # convert sediment to air
        eroded_mask = np.logical_and(
            ~np.in1d(self.materialField.data, airIndex), ~material_flags)
        self.materialField.data[eroded_mask] = airIndex

        return

    def solve(self, dt):
        if comm.rank == 0 and self.verbose:
            purple = "\033[0;35m"
            endcol = "\033[00m"
            print(purple +
                  "Processing surface with BasicHillSlopeDiffsuion2d" + endcol)

        self.SurfaceVeloEval(mesh=self.mesh, velocityField=self.velocityField)
        self.gridt[3:5, :] = comm.allreduce(self.gridt[3:5, :], op=_MPI.SUM)

        comm.barrier()
        if comm.rank == 0:
            self.SurfaceProcess(dt)
        self.gridt[1, :] = comm.bcast(self.gridt[1, :], root=0)
        comm.barrier()
        self.ErosionAndSedimentation()
        comm.barrier()

        if comm.rank == 0 and self.verbose:
            purple = "\033[0;35m"
            endcol = "\033[00m"
            print(purple +
                  "Processing surface with BasicHillSlopeDiffsuion2d" + endcol)
        return
