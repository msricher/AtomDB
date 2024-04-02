# This file is part of AtomDB.
#
# AtomDB is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# AtomDB is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License
# along with AtomDB. If not, see <http://www.gnu.org/licenses/>.

r"""AtomDB, a database of atomic and ionic properties."""

from dataclasses import dataclass, field, asdict

from glob import glob

from importlib import import_module

import json

from numbers import Integral

from os import makedirs, path

from msgpack import packb, unpackb

from msgpack_numpy import encode, decode

import numpy as np

from numpy import ndarray

from scipy.interpolate import CubicSpline

from atomdb.utils import DEFAULT_DATASET, DEFAULT_DATAPATH
from atomdb.periodic import Element, element_symbol


__all__ = [
    "Species",
    "compile",
    "dump",
    "load",
    "raw_datafile",
]


def default_vector():
    r"""Default factory for 1-dimensional ``np.ndarray``."""

    return np.zeros(0).reshape(0)


def default_matrix():
    r"""Default factory for 2-dimensional ``np.ndarray``."""
    return np.zeros(0).reshape(1, 0)


def scalar(method):
    r"""Expose a SpeciesData field."""
    name = method.__name__

    @property
    def wrapper(self):
        rf"""{method.__doc__}"""
        return getattr(self._data, name)

    return wrapper


def spline(method):
    r"""Expose a SpeciesData field via the ``DensitySpline`` interface."""
    name = method.__name__.removesuffix("_func")

    def wrapper(self, spin="t", index=None):
        rf"""{method.__doc__}"""
        # Validate `spin` variable
        if spin not in ("t", "a", "b", "m"):
            raise ValueError(
                f"Invalid `spin` parameter '{spin}'; "
                "choose one of ('t'| 'a' | 'b' | 'm')"
            )

        # Set names for {a,b,tot} arrays
        name_tot = f"{name}_tot"
        if self.spinpol == 1:
            name_a, name_b = f"mo_{name}_a", f"mo_{name}_b"
        else:
            name_a, name_b = f"mo_{name}_b", f"mo_{name}_a"

        # Extract arrays
        if index is None and spin == "t":
            arr = getattr(self._data, name_tot)
        elif spin == "t":
            arr = getattr(self._data, name_a) + getattr(self._data, name_b)
        elif spin == "a":
            arr = getattr(self._data, name_a)
        elif spin == "b":
            arr = getattr(self._data, name_b)
        elif spin == "m":
            arr = getattr(self._data, name_a) - getattr(self._data, name_b)

        # Return cubic spline
        return DensitySpline(
            self._data.rs,
            arr[... if index is None else index].sum(axis=0),
        )

    return wrapper


class DensitySpline:
    r"""Interpolate density using a cubic spline over a 1-D grid."""

    def __init__(self, x, y, log=False):
        r"""Initialize the CubicSpline instance."""
        self._log = log
        self._obj = CubicSpline(
            x,
            np.log(y) if log else y,
            axis=0,
            bc_type="not-a-knot",
            extrapolate=True,
        )

    def __call__(self, x, deriv=0):
        r"""
        Compute the interpolation at some x-values.

        Parameters
        ----------
        x: ndarray(M,)
            Points to be interpolated.
        deriv: int, default=0
            Order of spline derivative to evaluate. Must be 0, 1, or 2.

        Returns
        -------
        ndarray(M,)
            Interpolated values (1-D array).

        """
        if not (0 <= deriv <= 2):
            raise ValueError(
                f"Invalid derivative order {deriv}; must be 0 <= `deriv` <= 2"
            )
        elif self._log:
            y = np.exp(self._obj(x))
            if deriv == 1:
                # d(ρ(r)) = d(log(ρ(r))) * ρ(r)
                dlogy = self._obj(x, nu=1)
                y = dlogy.flatten() * y
            elif deriv == 2:
                # d^2(ρ(r)) = d^2(log(ρ(r))) * ρ(r) + [d(ρ(r))]^2/ρ(r)
                dlogy = self._obj(x, nu=1)
                d2logy = self._obj(x, nu=2)
                y = d2logy.flatten() * y + dlogy.flatten() ** 2 * y
        else:
            y = self._obj(x, nu=deriv)
        return y


class JSONEncoder(json.JSONEncoder):
    r"""JSON encoder handling simple `numpy.ndarray` objects."""

    def default(self, obj):
        r"""Default encode function."""
        if isinstance(obj, ndarray):
            return obj.tolist()
        else:
            return JSONEncoder.default(self, obj)


@dataclass(eq=False, order=False)
class SpeciesData:
    r"""Database entry fields for atomic and ionic species."""
    # Species info
    elem: str = field()
    atnum: int = field()
    basis: str = field()
    nelec: int = field()
    nspin: int = field()
    nexc: int = field()

    # Scalar energy and CDFT-related properties
    energy: float = field(default=None)
    ip: float = field(default=None)
    mu: float = field(default=None)
    eta: float = field(default=None)

    # Radial grid
    rs: ndarray = field(default_factory=default_vector)

    # Orbital energies
    mo_energy_a: ndarray = field(default_factory=default_vector)
    mo_energy_b: ndarray = field(default_factory=default_vector)

    # Orbital occupations
    mo_occs_a: ndarray = field(default_factory=default_vector)
    mo_occs_b: ndarray = field(default_factory=default_vector)

    # Orbital densities
    mo_dens_a: ndarray = field(default_factory=default_matrix)
    mo_dens_b: ndarray = field(default_factory=default_matrix)
    dens_tot: ndarray = field(default_factory=default_matrix)

    # Orbital density gradients
    mo_d_dens_a: ndarray = field(default_factory=default_matrix)
    mo_d_dens_b: ndarray = field(default_factory=default_matrix)
    d_dens_tot: ndarray = field(default_factory=default_matrix)

    # Orbital density Laplacian
    mo_dd_dens_a: ndarray = field(default_factory=default_matrix)
    mo_dd_dens_b: ndarray = field(default_factory=default_matrix)
    dd_dens_tot: ndarray = field(default_factory=default_matrix)

    # Orbital kinetic energy densities
    mo_ked_a: ndarray = field(default_factory=default_matrix)
    mo_ked_b: ndarray = field(default_factory=default_matrix)
    ked_tot: ndarray = field(default_factory=default_matrix)


class Species(Element):
    r"""Properties of atomic and ionic species."""

    def __init__(self, dataset, fields, spinpol=1):
        r"""Initialize a ``Species`` instance."""
        self._dataset = dataset.lower()
        self._data = SpeciesData(**fields)
        self.spinpol = spinpol
        Element.__init__(self, self._data.atnum)

    def get_docstring(self):
        r"""Docstring of the species' dataset."""
        return import_module(f"atomdb.datasets.{self._dataset}").__doc__

    def to_dict(self):
        r"""Return the dictionary representation of the Species instance."""
        return asdict(self._data)

    def to_json(self):
        r"""Return the JSON string representation of the Species instance."""
        return json.dumps(asdict(self._data), cls=JSONEncoder)

    @property
    def dataset(self):
        r"""Dataset."""
        return self._dataset

    @property
    def charge(self):
        r"""Charge."""
        return self._data.atnum - self._data.nelec

    @property
    def nspin(self):
        r"""Spin number :math:`N_S = N_α - N_β`."""
        self._data.nspin * self._spinpol

    @property
    def mult(self):
        r"""Multiplicity :math:`M = \left|N_S\right| + 1`."""
        self._data.nspin + 1

    @property
    def spinpol(self):
        r"""Spin polarization direction (±1) of the species."""
        return self._spinpol

    @spinpol.setter
    def spinpol(self, spinpol):
        r"""Spin polarization direction setter."""
        if not isinstance(spinpol, Integral):
            raise TypeError("`spinpol` attribute must be an integral type")

        spinpol = int(spinpol)

        if abs(spinpol) != 1:
            raise ValueError("`spinpol` must be +1 or -1")

        self._spinpol = spinpol

    @scalar
    def nexc(self):
        r"""Excitation number."""
        pass

    @scalar
    def energy(self):
        r"""Energy."""
        pass

    @scalar
    def ip(self):
        r"""Ionization potential."""
        pass

    @scalar
    def mu(self):
        r"""Chemical potential."""
        pass

    @scalar
    def eta(self):
        r"""Chemical hardness."""
        pass

    @spline
    def dens_func(self):
        r"""
        Return a cubic spline of the electronic density.

        Parameters
        ----------
        spin : str, default="ab"
            Type of occupied spin orbitals.
            Can be either "t" (for alpha + beta), "a" (for alpha),
            "b" (for beta), or "m" (for alpha - beta).
        index : sequence of int, optional
            Sequence of integers representing the spin orbitals.
            These are indexed from 0 to the number of basis functions.
            By default, all orbitals of the given spin(s) are included.
        log : bool, default=False
            Whether the logarithm of the density is used for interpolation.

        Returns
        -------
        DensitySpline
            A DensitySpline instance for the density and its derivatives.
            Given a set of radial points, it can evaluate densities and
            derivatives up to order 2.

        """
        pass

    @spline
    def dd_dens_func(self):
        r"""
        Return a cubic spline of the electronic density Laplacian.

        Parameters
        ----------
        spin : str, default="ab"
            Type of occupied spin orbitals.
            Can be either "t" (for alpha + beta), "a" (for alpha),
            "b" (for beta), or "m" (for alpha - beta).
        index : sequence of int, optional
            Sequence of integers representing the spin orbitals.
            These are indexed from 0 to the number of basis functions.
            By default, all orbitals of the given spin(s) are included.
        log : bool, default=False
            Whether the logarithm of the density is used for interpolation.

        Returns
        -------
        DensitySpline
            A DensitySpline instance for the density and its derivatives.
            Given a set of radial points, it can evaluate densities and
            derivatives up to order 2.

        """
        pass

    @spline
    def ked_func(self):
        r"""
        Return a cubic spline of the kinetic energy density.

        Parameters
        ----------
        spin : str, default="ab"
            Type of occupied spin orbitals.
            Can be either "t" (for alpha + beta), "a" (for alpha),
            "b" (for beta), or "m" (for alpha - beta).
        index : sequence of int, optional
            Sequence of integers representing the spin orbitals.
            These are indexed from 0 to the number of basis functions.
            By default, all orbitals of the given spin(s) are included.
        log : bool, default=False
            Whether the logarithm of the density is used for interpolation.

        Returns
        -------
        DensitySpline
            A DensitySpline instance for the density and its derivatives.
            Given a set of radial points, it can evaluate densities and
            derivatives up to order 2.

        """
        pass


def compile(
    elem, charge, mult,
    nexc=0,
    dataset=DEFAULT_DATASET,
    datapath=DEFAULT_DATAPATH,
):
    r"""Compile an atomic or ionic species into the AtomDB database."""
    # Ensure directories exist
    makedirs(path.join(datapath, dataset.lower(), "db"), exist_ok=True)
    makedirs(path.join(datapath, dataset.lower(), "raw"), exist_ok=True)
    # Import the compile script for the appropriate dataset
    submodule = import_module(f"atomdb.datasets.{dataset}.run")
    # Compile the Species instance and dump the database entry
    species = submodule.run(elem, charge, mult, nexc, dataset, datapath)
    dump(species, datapath=datapath)


def dump(*species, datapath=DEFAULT_DATAPATH):
    r"""Dump the Species instance(s) to a MessagePack file in the database."""
    for s in species:
        fn = datafile(s.elem, s.charge, s.mult,
                      nexc=s.nexc, dataset=s.dataset, datapath=datapath)
        with open(fn, "wb") as f:
            f.write(packb(asdict(s._data), default=encode))


def load(
    elem, charge, mult,
    nexc=0,
    dataset=DEFAULT_DATASET,
    datapath=DEFAULT_DATAPATH,
):
    r"""Load one or many atomic or ionic species from the AtomDB database."""
    fn = datafile(
        elem, charge, mult,
        nexc=nexc,
        dataset=dataset,
        datapath=datapath,
    )
    if Ellipsis in (elem, charge, mult, nexc):
        obj = []
        for file in glob(fn):
            with open(file, "rb") as f:
                obj.append(
                    Species(dataset, unpackb(f.readall(), object_hook=decode))
                )
    else:
        with open(fn) as f:
            obj = Species(dataset, unpackb(f.readall(), object_hook=decode))
    return obj


def datafile(
    elem, charge, mult,
    nexc=0,
    dataset=DEFAULT_DATASET,
    datapath=DEFAULT_DATAPATH,
):
    r"""Return the name of the database file for a species."""
    elem = "*" if elem is Ellipsis else element_symbol(elem)
    charge = "*" if charge is Ellipsis else f"{charge:03d}"
    mult = "*" if mult is Ellipsis else f"{mult:03d}"
    nexc = "*" if nexc is Ellipsis else f"{nexc:03d}"
    return path.join(
        datapath, dataset.lower(), "db",
        f"{elem}_{charge}_{mult}_{nexc}.msg"
    )


def raw_datafile(
    suffix, elem, charge, mult,
    nexc=0,
    dataset=DEFAULT_DATASET,
    datapath=DEFAULT_DATAPATH,
):
    r"""Return the name of the database file for a species."""
    elem = "*" if elem is Ellipsis else element_symbol(elem)
    charge = "*" if charge is Ellipsis else f"{charge:03d}"
    mult = "*" if mult is Ellipsis else f"{mult:03d}"
    nexc = "*" if nexc is Ellipsis else f"{nexc:03d}"
    return path.join(
        datapath, dataset.lower(), "raw",
        f"{elem}_{charge}_{mult}_{nexc}{suffix}"
    )