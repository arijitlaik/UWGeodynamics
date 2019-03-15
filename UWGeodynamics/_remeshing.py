import numpy as np
from UWGeodynamics import non_dimensionalise as nd
from .Underworld_extended import MeshVariable
from scipy.interpolate import interp1d
import copy


class ReMesher(object):

    def __init__(self, Model, x, y, z, reset=False, adaptive=False):

        self.mesh = Model.mesh
        self.initial_mesh = copy.copy(self.mesh)
        self.x = x
        self.y = y
        self.z = z
        self.reset = reset

        if adaptive:
            Model.post_solve_functions["Remesher"] = self.remesh

    def reset_mesh(self):
        with self.mesh.deform_mesh():
            self.mesh.data[...] = self.initial_mesh.data[...]

    def remesh(self):

        if self.reset:
            self.reset_mesh()

        nx, ny = self.mesh.elementRes

        def new_points(intervals, elements):
            pts = []
            for idx in range(len(intervals) - 1):
                pts.append(np.linspace(intervals[idx], intervals[idx + 1], elements[idx] + 1))
            pts = np.unique(np.hstack(pts))
            pts.sort()
            return pts

        def check_vals(intervals, elements, axis):
            if ((intervals[0] != self.mesh.minCoord[axis]) or
                (intervals[-1] != self.mesh.maxCoord[axis])):
                raise ValueError("""Intervals do not match mesh extent""")

            if np.sum(np.array(elements)) != self.mesh.elementRes[axis]:
                raise ValueError("""Total nb of elements do not match the nb of elements in the mesh""")

        def new_points_from_field(field, axis):
            nx, ny = self.mesh.elementRes
            A = field.data.reshape((nx + 1, ny + 1))
            B = A.max(axis=axis)
            C = B / B.max()
            D = C.cumsum()
            E = D - D[0]
            E = E / E.max()
            if axis == 1:
                coords = self.mesh.data[:, axis].reshape((nx + 1, ny + 1))[:, -1]
            if axis == 0:
                coords = self.mesh.data[:, axis].reshape((nx + 1, ny + 1))[0, :]
            ys = np.linspace(0., 1.0, self.mesh.elementRes[axis] + 1)
            func = interp1d(E, coords, fill_value=(coords[0], coords[-1]),
                            bounds_error=False)
            vals = func(ys)
            return vals

        if self.x:

            if isinstance(self.x, tuple):
                intervals, elements = self.x
                intervals = [nd(val) for val in intervals]
                elements = [nd(val) for val in elements]
                check_vals(intervals, elements, 0)
                pts = new_points(intervals, elements)
            if isinstance(self.x, MeshVariable):
                pts = new_points_from_field(self.x, 0)

            with self.mesh.deform_mesh():
                new_vals = np.tile(pts, ny + 1)
                self.mesh.data[:, 0] = new_vals[self.mesh.data_nodegId.flatten()]

        if self.y:

            if isinstance(self.y, tuple):
                pts = new_points(intervals, elements)
                intervals, elements = self.y
                intervals = [nd(val) for val in intervals]
                elements = [nd(val) for val in elements]
                check_vals(intervals, elements, 1)
            if isinstance(self.y, MeshVariable):
                pts = new_points_from_field(self.y, 1)

            with self.mesh.deform_mesh():
                vals = np.repeat(pts, nx + 1)
                self.mesh.data[:, 1] = vals[self.mesh.data_nodegId.flatten()]
        return self.mesh

