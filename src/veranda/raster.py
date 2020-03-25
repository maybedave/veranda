import os
import abc
import sys
import ogr
import osr
import warnings
import cartopy
import pandas as pd
import numpy as np
import xarray as xr
import shapely.wkt
from shapely.geometry import Point
from matplotlib import cm
from collections import OrderedDict

from veranda.io.geotiff import GeoTiffFile
from veranda.io.netcdf import NcFile
from veranda.io.timestack import GeoTiffRasterTimeStack
from veranda.io.timestack import NcRasterTimeStack
from veranda.plot import RasterStackSlider

from veranda.errors import DataTypeMismatch
from veranda.errors import DimensionsMismatch

from geospade.definition import RasterGeometry
from geospade.definition import RasterGrid
from geospade.definition import _any_geom2ogr_geom
from geospade.spatial_ref import SpatialRef
from geospade.operation import ij2xy
from geospade.operation import rel_extent
from geospade.operation import coordinate_traffo


# TODO: can we represent a rotated array with xarray?
# ToDO: change band to list of bands
def convert_data_coding(data, coder, coder_kwargs=None, band=None):
    """
    Converts data values via a given coding function.
    A band/data variable (`band`) needs to be given if one works with xarray data sets.

    Parameters
    ----------
    data : numpy.ndarray or xarray.Dataset
        Array-like object containing image pixel values.
    coder : function
        Coding function, which expects NumPy/Dask arrays.
    coder_kwargs : dict, optional
        Keyword arguments for the coding function.
    band : str or int, optional
        Band/data variable of xarray data set.

    Returns
    -------
    numpy.ndarray or xarray.Dataset
        Array-like object containing coded image pixel values.
    """

    code_kwargs = {} if coder_kwargs is None else coder_kwargs

    if isinstance(data, xr.Dataset):
        if band is None:
            err_msg = "A band name has to be specified for coding the data."
            raise KeyError(err_msg)
        data[band].data = coder(data[band].data, **code_kwargs)
        return data
    elif isinstance(data, np.ndarray):
        return coder(data, **code_kwargs)
    else:
        err_msg = "Data type is not supported for coding the data."
        raise Exception(err_msg)


# TODO: where should we put this?
def convert_data_type(data, *coord_args, data_type="numpy", band=None, dim_names=None):
    """
    Converts `data` into an array-like object defined by `data_type`. It accepts NumPy arrays or Xarray data sets and
    can convert to Numpy arrays, Xarray data sets or Pandas data frames.

    Parameters
    ----------
    data : numpy.ndarray or xarray.Dataset
        Array-like object containing image pixel values.
    data_type : str
        Data type of the returned array-like structure. It can be:
            - 'xarray': converts data to an xarray.Dataset
            - 'numpy': convert data to a numpy.ndarray (default)
            - 'dataframe': converts data to a grouped pandas.DataFrame
    *coord_args : unzipped tuple of lists
        Coordinate arguments defined as a list, e.g.:
        - *(xs, ys, timestamps): contains a list of world system coordinates in X direction, a list of world
        system coordinates in Y direction and a list of timestamps.
    band : int or str, optional
        Band number or data variable name to select from an xarray data set (relevant for an xarray -> numpy conversion).
    dim_names : list of str, optional
        List of dimension names having the same length as `*coord_args`. The default behaviour is ['y', 'x', 'time']
        ATTENTION: The order needs to follow the same order as `*coord_args`!

    Returns
    -------
    numpy.ndarray or xarray.Dataset
        Array-like object containing image pixel values.
    """
    if dim_names is None:
        dim_names = ['y', 'x', 'time']

    n_coord_args = len(coord_args)
    n_dim_names = len(dim_names)
    if n_coord_args != n_dim_names:
        err_msg = "Number of coordinate arguments ({}) " \
                  "does not match number of dimension names ({}).".format(n_coord_args, n_dim_names)
        raise Exception(err_msg)

    if data_type == "xarray":
        if isinstance(data, np.ndarray):
            coords = OrderedDict()
            for i, dim_name in enumerate(dim_names):
                coords[dim_name] = coord_args[i]
            xr_ar = xr.DataArray(data, coords=coords, dims=dim_names)
            conv_data = xr.Dataset(data_vars={band: xr_ar})
        elif isinstance(data, xr.Dataset):
            conv_data = data
        else:
            raise DataTypeMismatch(type(data), data_type)
    elif data_type == "numpy":
        if isinstance(data, xr.Dataset):
            if band is None:
                err_msg = "Band/label/data variable argument is not specified."
                raise Exception(err_msg)
            conv_data = np.array(data[band].data)
        elif isinstance(data, np.ndarray):
            conv_data = data
        else:
            raise DataTypeMismatch(type(data), data_type)
    elif data_type == "dataframe":
        xr_ds = convert_data_type(data, 'xarray', *coord_args, band=band, dim_names=dim_names)
        conv_data = xr_ds.to_dataframe()
    else:
        raise DataTypeMismatch(type(data), data_type)

    return conv_data


# ToDO: initialise with raster geometry or rows, cols, sref, gt?
class RasterData(metaclass=abc.ABCMeta):
    """
    This class represents geo-referenced raster data. Its two main components are a geometry and data:

    The geometry defines all spatial properties of the data like extent, pixel size,
    location and orientation in a spatial reference system (class `RasterGeometry`).

    The other component is data, which is an array-like object that contains the actual values of the raster file.
    Every `RasterData` object stores an instance of some IO class (e.g., `GeoTiffFile`, `NcFile`), which is used for
    IO operations.

    At the moment `RasterData` offers all basic functionalities for the child classes `RasterLayer` and `RasterStack`.
    """

    def __init__(self, n_rows, n_cols, sref, geotrans, data=None, data_type="numpy", io=None, label=None, parent=None):
        """
        Basic constructor of class `RasterData`.

        Parameters
        ----------
        n_rows : int
            Number of pixel rows.
        n_cols : int
            Number of pixel columns.
        sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference
            Instance representing the spatial reference of the geometry.
        geotrans : 6-tuple, optional
            GDAL geotransform tuple.
        data : numpy.ndarray or xarray.Dataset, optional
            Array-like object containing image pixel values.
        data_type : str, optional
            Data type of the returned array-like structure. It can be:
            - 'xarray': converts data to an xarray.Dataset
            - 'numpy': convert data to a numpy.ndarray (default)
        io : object, optional
            Instance of an IO Class that is associated with a file that contains the data.
        label : str or int, optional
            Defines a band or a data variable name.
        parent : RasterData, optional
            Parent `RasterData` instance.
        """

        self.raster_geom = RasterGeometry(n_rows, n_cols, sref, geotrans)

        # set and check data properties
        if data is not None:
            self._check_data(data)
        self._data = data
        self.data_type = data_type

        self.io = io
        self.label = label
        self.parent = parent

    @property
    @abc.abstractmethod
    def data(self):
        """
        numpy.ndarray or xarray.Dataset : Retrieves data in the data/array format as defined by the class variable
        `data_type`.
        """
        pass

    @data.setter
    def data(self, data):
        """
        Sets internal data.

        Parameters
        ----------
        data : numpy.ndarray or xarray.Dataset
            Array-like object containing image pixel values.
        """
        pass

    @property
    def parent_root(self):
        """ RasterData : Finds and returns the root/original parent `RasterData`. """
        raster_data = self
        while raster_data.parent is not None:
            raster_data = raster_data.parent
        return raster_data

    @classmethod
    @abc.abstractmethod
    def from_array(cls, sref, geotrans, data, **kwargs):
        """
        Creates a `RasterData` object from an array-like object.

        Parameters
        ----------
        sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference
            Instance representing the spatial reference of the geometry.
        geotrans : 6-tuple, optional
            GDAL geotransform tuple.
        data : numpy.ndarray or xarray.Dataset, optional
            Array-like object containing image pixel values.
        **kwargs
            Keyword arguments for `RasterData` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterData
        """
        pass

    @classmethod
    @abc.abstractmethod
    def from_filepath(cls, filepaths, read=False, read_kwargs=None, io_class=None, io_kwargs=None, **kwargs):
        """
        Creates a `RasterData` object from a filepath.

        Parameters
        ----------
        filepaths : str or list of str
            Full file path(s) to the raster file.
        read : bool, optional
            If true, data is read and assigned to the `RasterData` class (default is False).
        read_kwargs : dict, optional
            Keyword arguments for the reading function of the IO class.
        io_class : class, optional
            IO class.
        io_kwargs : dict, optional
            Keyword arguments for IO class initialisation.
        **kwargs
            Keyword arguments for `RasterData` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterData
        """
        pass

    @classmethod
    @abc.abstractmethod
    def from_io(cls, io, read=False, read_kwargs=None, **kwargs):
        """
        Creates a `RasterData` object from an IO class instance.

        Parameters
        ----------
        io : GeoTiffFile or NcFile or object
            IO class instance.
        read : bool, optional
            If true, data is read and assigned to the `RasterData` class.
        read_kwargs : dict, optional
            Keyword arguments for the reading function of the IO class.
        **kwargs
            Keyword arguments for `RasterData` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterData
        """
        pass

    @classmethod
    def from_file(cls, arg, read=False, read_kwargs=None, io_class=None, io_kwargs=None, **kwargs):
        """
        Creates a `RasterData` object from filepaths or an IO class instance.

        Parameters
        ----------
        arg : str or list of str or object
            Full file path(s) to the raster file or IO class instance.
        read : bool, optional
            If true, data is read and assigned to the `RasterData` class (default is False).
        read_kwargs : dict, optional
            Keyword arguments for the reading function of the IO class.
        io_class : class, optional
            IO class.
        io_kwargs : dict, optional
            Keyword arguments for IO class initialisation.
        **kwargs
            Keyword arguments for `RasterData` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterData
        """

        if isinstance(arg, str) or (isinstance(arg, list) and isinstance(arg[0], str)):
            return cls.from_filepath(arg, read=read, read_kwargs=read_kwargs,
                                     io_class=io_class, io_kwargs=io_kwargs, **kwargs)
        else:  # if it is not a string or a list of strings it is assumed that it is an IO class instance
            return cls.from_io(arg, read=read, read_kwargs=read_kwargs, **kwargs)

    @_any_geom2ogr_geom
    def crop(self, geom, sref=None, slices=None, apply_mask=False, buffer=0, inplace=False):
        """
        Crops the data by a geometry. In addition, a mask can be applied with a certain buffer.
        `inplace` determines, whether a new object is returned or the cropping happens on the object instance.

        Parameters
        ----------
        geom : geospade.definition.RasterGeometry or ogr.Geometry or shapely.geometry or list or tuple
            Geometry defining the data extent of interest.
        sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference, optional
            Spatial reference of the geometry.
            Has to be given if the spatial reference is different than the spatial reference of the raster data.
            Note: `sref` is used in the decorator `_any_geom2ogr_geom`.
        slices: tuple, optional
            Additional array slices for all the dimensions coming before the spatial indexing via pixels.
        apply_mask : bool, optional
            If true, a mask is applied for data points being not inside the given geometry (default is False).
        buffer : int, optional
            Pixel buffer for crop geometry (default is 0).
        inplace : bool, optional
            If true, the current instance will be modified.
            If false, a new `RasterData` instance will be created (default).

        Returns
        -------
        RasterData
            `RasterData` object only containing data within the intersection.
            If the `RasterData` and the given geometry do not intersect, None is returned.
        """

        # create new geometry from the intersection
        intsct_raster_geom = self.raster_geom & geom

        if intsct_raster_geom is not None and self._data is not None:
            min_col, min_row, max_col, max_row = rel_extent((self.raster_geom.ul_x, self.raster_geom.ul_y),
                                                            intsct_raster_geom.inner_extent,
                                                            x_pixel_size=self.raster_geom.x_pixel_size,
                                                            y_pixel_size=self.raster_geom.y_pixel_size)
            px_slices = (slice(min_row, max_row+1), slice(min_col, max_col+1))
            raster_data = self._load_array(px_slices, slices=slices, inplace=inplace)
            if apply_mask:
                mask = raster_data.raster_geom.create_mask(geom, buffer=buffer)
                # +1 because max row and column need to be included
                raster_data.apply_mask(mask[min_row:(max_row+1), min_col:(max_col+1)], inplace=True)
            return raster_data
        else:
            return None

    def load(self, band=None, io=None, read_kwargs=None, data_type=None, decode=True, decode_kwargs=None,
             inplace=False):
        """
        Reads data from disk and assigns the resulting array to the
        `self.data` attribute.

        Parameters
        ----------
        band : str or int, optional
            Defines a band or a data variable name. The default behaviour is to take `self.label`.
            If `self.label` is also None, then all available bands are loaded.
        io : GeoTiffFile or NcFile or object, optional
            IO class instance.
        read_kwargs : dict, optional
            Keyword arguments for reading function of IO class.
        data_type : str, optional
            Data type of the returned array-like structure (default is None -> class variable `data_type` is used).
            It can be:
                - 'xarray': loads data as an xarray.DataSet
                - 'numpy': loads data as a numpy.ndarray
        decode : bool, optional
            If true, data is decoded according to the class method `decode`.
        decode_kwargs: dict, optional
            Keyword arguments for the decoder.
        inplace : bool, optional
            If true, the current `RasterData` instance will be modified.
            If false, the loaded data will be returned (default).

        Returns
        -------
        RasterData :
            `RasterData` object containing loaded data.
        """

        io = self.io if io is None else io
        if io is None:
            err_msg = "An IO instance has to be given to load data from disk."
            raise Exception(err_msg)

        read_kwargs = read_kwargs if read_kwargs is not None else {}
        data_type = data_type if data_type is not None else self.data_type

        if band is not None:
            read_kwargs.update({'band': band})
        elif self.label is not None:
            read_kwargs.update({'band': self.label})
        else:
            read_kwargs.update({'band': None})

        if decode:
            read_kwargs.update({'decoder': self.decode})
            read_kwargs.update({'decode_kwargs': decode_kwargs})

        data = io.read(**read_kwargs)
        if data is None:
            err_msg = "Could not read data."
            raise Exception(err_msg)

        self._check_data(data)

        col = read_kwargs.pop("col", None)
        row = read_kwargs.pop("row", None)
        if col is not None and row is not None:  # cut geometry according to loaded data
            col_size = read_kwargs.get("col_size", 1)
            row_size = read_kwargs.get("row_size", 1)
            max_row, max_col = row + row_size - 1, col + col_size - 1 # -1 because of Python indexing
            px_extent = (row, max_row, col, max_col)
            intsct_raster_geom = self.raster_geom.intersection_by_pixel(px_extent, inplace=False)
        else:
            intsct_raster_geom = self.raster_geom

        data = self._convert_data_type(data=data, data_type=data_type, raster_geom=intsct_raster_geom)
        raster_data = self.from_array(intsct_raster_geom.sref, intsct_raster_geom.geotrans, data=data,
                                      data_type=data_type, parent=self, io=self.io, label=self.label)

        if inplace:
            self.data_type = raster_data.data_type
            self._data = raster_data._data
            self.raster_geom = raster_data.raster_geom
        else:
            return raster_data

    def load_by_coords(self, x, y, sref=None, slices=None, band=None, data_type=None, px_origin="ul", decode=True,
                       decode_kwargs=None, inplace=False, **kwargs):
        """
        Reads data/one pixel according to the given coordinates.

        Parameters
        ----------
        x : float
            World system coordinate in x direction.
        y : float
            World system coordinate in y direction.
        sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference, optional
            Spatial reference of the coordinates.
            Has to be given if the spatial reference is different than the spatial reference of the raster data.
            Note: `sref` is used in the decorator `_any_geom2ogr_geom`.
        slices : tuple, optional
            Additional array slices for all the dimensions coming before the spatial indexing via pixels.
        band : str or int, optional
            Defines a band or a data variable name. The default behaviour is to take `self.label`.
            If `self.label` is also None, then all available bands are loaded.
        data_type : str, optional
            Data type of the returned array-like structure (default is None -> class variable `data_type` is used).
            It can be:
                - 'xarray': loads data as an xarray.DataSet
                - 'numpy': loads data as a numpy.ndarray
        px_origin : str, optional
            Defines the world system origin of the pixel. It can be:
                - upper left ("ul", default)
                - upper right ("ur")
                - lower right ("lr")
                - lower left ("ll")
                - center ("c")
        decode : bool, optional
            If true, data is decoded according to the class method `decode` (default is True).
        decode_kwargs: dict, optional
            Keyword arguments for the decoder.
        inplace : bool, optional
            If true, the current `RasterData` instance will be modified.
            If false, the loaded data will be returned (default).
        ** kwargs
            Keyword arguments for `load` function, i.e. `io`.

        Returns
        -------
        RasterData :
            `RasterData` object containing data referring to the given coordinates.
        """

        read_kwargs = kwargs.get("read_kwargs", {})
        data_type = data_type if data_type is not None else self.data_type

        poi = ogr.Geometry(ogr.wkbPoint)
        poi.AddPoint(x, y)
        row, col = self.raster_geom.xy2rc(x, y, px_origin=px_origin, sref=sref)
        if self.parent_root.raster_geom.intersects(poi, sref=sref):  # point is within raster boundaries
            if self._data is None or not self.raster_geom.within(poi, sref=sref): # maybe it does not intersect because part of data is not loaded
                read_kwargs.update({"row": row})
                read_kwargs.update({"col": col})
                return self.load(band=band, read_kwargs=read_kwargs, data_type=data_type, inplace=inplace,
                                 decode=decode, decode_kwargs=decode_kwargs, **kwargs)
            else:
                px_slices = (slice(row, row+1), slice(col, col+1))
                return self._load_array(px_slices, slices=slices, band=band, data_type=data_type, inplace=inplace)
        else:
            wrn_msg = "The given coordinates do not intersect with the raster."
            warnings.warn(wrn_msg)
            return self

    @_any_geom2ogr_geom
    def load_by_geom(self, geom, sref=None, slices=None, band=None, data_type=None, apply_mask=False, decode=True,
                     decode_kwargs=None, buffer=0, inplace=False, **kwargs):
        """
        Reads data according to the given geometry/region of interest.

        Parameters
        ----------
        geom : geospade.definition.RasterGeometry or ogr.Geometry or shapely.geometry or list or tuple
            Other geometry used for cropping the data.
        sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference, optional
            Spatial reference of the coordinates.
            Has to be given if the spatial reference is different than the spatial reference of the raster data.
            Note: `sref` is used in the decorator `_any_geom2ogr_geom`.
        slices: tuple, optional
            Additional array slices for all the dimensions coming before the spatial indexing via pixels.
        band : str or int, optional
            Defines a band or a data variable name. The default behaviour is to take `self.label`.
            If `self.label` is also None, then all available bands are loaded.
        data_type : str, optional
            Data type of the returned array-like structure (default is None -> class variable `data_type` is used).
            It can be:
                - 'xarray': loads data as an xarray.DataSet
                - 'numpy': loads data as a numpy.ndarray
        apply_mask : bool, optional
            If true, a mask is applied for data points being not inside the given geometry (default is False).
        decode : bool, optional
            If true, data is decoded according to the class method `decode` (default is True).
        decode_kwargs: dict, optional
            Keyword arguments for the decoder.
        buffer : int, optional
            Pixel buffer for crop geometry (default is 0).
        inplace : bool, optional
            If true, the current `RasterData` instance will be modified.
            If false, the loaded data will be returned (default).
        ** kwargs
            Keyword arguments for `load` function, i.e. `io`.

        Returns
        -------
        RasterData :
            `RasterData` object containing data referring to the given geometry.
        """
        read_kwargs = kwargs.get("read_kwargs", {})
        data_type = data_type if data_type is not None else self.data_type

        intsct_raster_geom = self.raster_geom & geom
        min_col, min_row, max_col, max_row = rel_extent((self.raster_geom.parent_root.ul_x,
                                                         self.raster_geom.parent_root.ul_y),
                                                        intsct_raster_geom.inner_extent,
                                                        x_pixel_size=self.raster_geom.x_pixel_size,
                                                        y_pixel_size=self.raster_geom.y_pixel_size)
        row_size = max_row - min_row
        col_size = max_col - min_col

        if self.parent_root.raster_geom.intersects(geom, sref=sref):  # geometry intersects with raster boundaries
            if self._data is None or not self.raster_geom.within(geom, sref=sref):  # maybe it does not intersect because part of data is not loaded
                read_kwargs.update({"row": min_row})
                read_kwargs.update({"col": min_col})
                read_kwargs.update({"row_size": row_size})
                read_kwargs.update({"col_size": col_size})
                raster_data = self.load(band=band, read_kwargs=read_kwargs, data_type=data_type, inplace=inplace,
                                        decode=decode, decode_kwargs=decode_kwargs, **kwargs)
            else:
                # +1 because maximum row/column needs to be included
                px_slices = (slice(min_row, max_row+1), slice(min_col, max_col+1))
                raster_data = self._load_array(px_slices, slices=slices, band=band, data_type=data_type,
                                               inplace=inplace)

            if apply_mask:
                mask = raster_data.raster_geom.create_mask(geom, buffer=buffer)
                # +1 because max row and column need to be included
                raster_data.apply_mask(mask[min_row:(max_row+1), min_col:(max_col+1)], inplace=True)

            return raster_data
        else:
            wrn_msg = "The given geometry does not intersect with the raster."
            warnings.warn(wrn_msg)
            return self

    def load_by_pixel(self, row, col, row_size=1, col_size=1, slices=None, band=None, data_type=None, decode=True,
                      decode_kwargs=None, inplace=False, **kwargs):
        """
        Reads data according to the given pixel extent.

        Parameters
        ----------
        row : int
            Pixel row number.
        col : int
            Pixel column number.
        row_size : int, optional
            Number of rows to read (default is 1).
        col_size : int, optional
            Number of cols to read (default is 1).
        slices: tuple, optional
            Additional array slices for all the dimensions coming before the spatial indexing via pixels.
        band : str or int, optional
            Defines a band or a data variable name. The default behaviour is to take `self.label`.
            If `self.label` is also None, then all available bands are loaded.
        data_type : str, optional
            Data type of the returned array-like structure (default is None -> class variable `data_type` is used).
            It can be:
                - 'xarray': loads data as an xarray.Dataset
                - 'numpy': loads data as a numpy.ndarray
        decode : bool, optional
            If true, data is decoded according to the class method `decode` (default is True).
        decode_kwargs: dict, optional
            Keyword arguments for the decoder.
        inplace : bool, optional
            If true, the current `RasterData` instance will be modified.
            If false, the loaded data will be returned (default).
        ** kwargs
            Keyword arguments for `load` function, i.e. `io`.

        Returns
        -------
        RasterData :
            `RasterData` object containing data referring to the given pixel extent.
        """
        read_kwargs = kwargs.get("read_kwargs", {})
        data_type = data_type if data_type is not None else self.data_type

        min_row = row
        min_col = col
        max_row = min_row + row_size - 1  # -1 because 'crop_px_extent' acts on pixel indexes
        max_col = min_col + col_size - 1  # -1 because 'crop_px_extent' acts on pixel indexes
        min_row, min_col, max_row, max_col = self.raster_geom.crop_px_extent(min_row, min_col, max_row, max_col)
        if self._data is None:
            row_size = max_row - min_row + 1
            col_size = max_col - min_col + 1
            read_kwargs.update({"row": min_row})
            read_kwargs.update({"col": min_col})
            read_kwargs.update({"row_size": row_size})
            read_kwargs.update({"col_size": col_size})
            return self.load(band=band, read_kwargs=read_kwargs, data_type=data_type, inplace=inplace,
                             decode=decode, decode_kwargs=decode_kwargs, **kwargs)
        else:
            # +1 because max_row/max_col needs to be included
            px_slices = (slice(min_row, max_row+1), slice(min_col, max_col+1))
            return self._load_array(px_slices, slices=slices, band=band, data_type=data_type, inplace=inplace)

    def _load_array(self, px_slices, slices=None, band=None, data_type=None, inplace=False):
        """
        Reads/indexes array data from memory.

        Parameters
        ----------
        px_slices : 2-tuple
            Array slices containing row and and column slices for spatial indexing.
        slices: tuple, optional
            Additional array slices for all the dimensions coming before the spatial indexing via pixels.
        band : str or int, optional
            Defines a band or a data variable name. The default behaviour is to take `self.label`.
            If `self.label` is also None, then all available bands are loaded.
        data_type : str, optional
            Data type of the returned array-like structure (default is None -> class variable `data_type` is used).
            It can be:
                - 'xarray': loads data as an xarray.Dataset
                - 'numpy': loads data as a numpy.ndarray
        inplace : bool, optional
            If true, the current `RasterData` instance will be modified.
            If false, the data in memory will be returned (default).

        Returns
        -------
        RasterData :
            `RasterData` object containing data stored in memory.
        """
        data_type = self.data_type if data_type is None else data_type
        if band is None and self.label is not None:
            band = self.label

        if slices is not None:
            slices = (*slices, *px_slices)
        else:
            slices = px_slices

        if isinstance(self._data, np.ndarray):
            data = self._data[slices]
        elif isinstance(self._data, xr.Dataset):
            if band is None:
                bands = list(self._data.keys())
            else:
                bands = [band]
            data_ar = self._data[bands[0]][slices]
            data = data_ar.to_dataset()
            for band in bands[1:]:
                data_ar = self._data[band][slices]
                data = data.merge(data_ar.to_dataset())
        else:
            err_msg = "Data type is not supported for accessing and decoding the data."
            raise Exception(err_msg)

        min_row, max_row = px_slices[0].start, px_slices[0].stop
        min_col, max_col = px_slices[1].start, px_slices[1].stop
        px_extent = (min_row, min_col, max_row, max_col)
        intsct_raster_geom = self.raster_geom.intersection_by_pixel(px_extent, inplace=False)

        data = self._convert_data_type(data=data, data_type=data_type, raster_geom=intsct_raster_geom)
        raster_data = self.from_array(intsct_raster_geom.sref, intsct_raster_geom.geotrans, data=data,
                                      data_type=data_type, parent=self, io=self.io, label=self.label)

        if inplace:
            self.data_type = raster_data.data_type
            self._data = raster_data._data
            self.raster_geom = raster_data.raster_geom
        else:
            return raster_data

    @abc.abstractmethod
    def _convert_data_type(self, data=None, data_type=None, raster_geom=None):
        """
        Class wrapper for `convert_data_type` function.

        Parameters
        ----------
        data : numpy.ndarray or xarray.Dataset, optional
            2D/3D array-like object containing image pixel values.
        data_type : str, optional
            Data type of the returned array-like structure (default is None -> class variable `data_type` is used).
            It can be:
                - 'xarray': converts data to an xarray.Dataset
                - 'numpy': converts data to a numpy.ndarray
        raster_geom : `RasterGeometry`, optional
            Geometry in synergy with `data`. Needed to select the coordinate values along the data axis.

        Returns
        -------
        numpy.ndarray or xarray.Dataset
            Array-like object containing image pixel values.
        """
        pass

    def apply_mask(self, mask, data=None, band=None, inplace=False):
        """
        Applies a mask to given or internal data.

        Parameters
        ----------
        mask : numpy.ndarray
            2D or 3D Mask.
        data : numpy.ndarray or xarray.Dataset, optional
            Array-like object containing image pixel values. If None, then the class data is used.
        band : str or int, optional
            Defines a band or a data variable name. The default behaviour is to take `self.label`.
            If `self.label` is also None, then all available bands are taken.
        inplace : bool, optional
            If true, the current `RasterData` instance will be modified.
            If false, the data in memory will be returned (default).

        Returns
        -------
        numpy.ndarray or xarray.Dataset, optional
            Array-like object containing image pixel values and a numpy masked array.

        Notes
        -----
        Only 2 or 3 dimensional data is currently supported.
        """
        self._check_data(data)
        data = data if data is not None else self._data
        if band is None and self.label is not None:
            band = self.label

        n_dims_mask = len(mask.shape)
        if isinstance(data, np.ndarray):
            n_dims_data = len(data.shape)
            if n_dims_data == 3 and n_dims_mask == 2:  # mask needs to be replicated to match data dimensions
                mask = np.stack([mask] * data.shape[0], axis=0)
            elif n_dims_data != n_dims_mask:
                err_msg = "Mask ({}) and data ({}) dimensions mismatch.".format(n_dims_mask, n_dims_data)
                raise Exception(err_msg)
            data = np.ma.array(data, mask=mask)
        elif isinstance(data, xr.Dataset):
            n_dims_data = len(data.dims)
            if n_dims_data == 3 and n_dims_mask == 2:  # mask needs to be replicated to match data dimensions
                mask = np.stack([mask] * data.dims[0], axis=0)
            elif n_dims_data != n_dims_mask:
                err_msg = "Mask ({}) and data ({}) dimensions mismatch.".format(n_dims_mask, n_dims_data)
                raise Exception(err_msg)
            data_ar = data[band]
            data_ar.data = np.ma.array(data_ar.data, mask=mask)
            data = data_ar.to_dataset()
        else:
            err_msg = "Data type is not supported for accessing and decoding the data."
            raise Exception(err_msg)

        raster_data = self.from_array(self.raster_geom.sref, self.raster_geom.geotrans, data=data,
                                      data_type=self.data_type, parent=self, io=self.io, label=self.label)

        if inplace:
            self._data = raster_data._data
            return self
        else:
            return raster_data

    @abc.abstractmethod
    def write(self, *args, **kwargs):
        """
        Writes data to disk into (a) target file/s.

        Parameters
        ----------
        *args : Arguments needed to write data to disk (should contain filepath/s).
        **kwargs : Keyword arguments needed to write data to disk
        """
        pass

    def encode(self, data, **kwargs):
        """
        Encodes data.

        Parameters
        ----------
        data : numpy.ndarray or xarray.DataArray, optional
            Array-like object containing image pixel values.
        **kwargs : Keyword arguments for encoding function.

        Returns
        -------
        data : numpy.ndarray or xarray.DataArray, optional
            Encoded array.
        """
        return data

    def decode(self, data, **kwargs):
        """
        Decodes data.

        Parameters
        ----------
        data : numpy.ndarray or xarray.DataArray, optional
            Array-like object containing image pixel values.
        **kwargs : Keyword arguments for decoding function.

        Returns
        -------
        data : numpy.ndarray or xarray.DataArray, optional
            Decoded array.
        """
        return data

    @staticmethod
    def _check_data(data):
        """
        Checks array type and structure of data.

        Parameters
        ----------
        data : numpy.ndarray or xarray.DataArray, optional
            Array-like object containing image pixel values.

        Returns
        -------
        bool
            If true, the given data fulfills all requirements for a `RasterData` object.
        """

        if data is not None:
            if not isinstance(data, np.ndarray) and not isinstance(data, xr.Dataset):
                err_msg = "Data type is not supported for this class."
                raise Exception(err_msg)
            return True
        else:
            return False

    @staticmethod
    @abc.abstractmethod
    def _io_class_from_filepath(filepath):
        """
        Selects an IO class depending on the filepath/file ending.

        Parameters
        ----------
        filepath : str
            Full file path of the output file.

        Returns
        -------
        io_class : class
            IO class.
        file_type : str
            Type of file.
        """
        pass


class RasterLayer(RasterData):
    """ Raster data class for one raster layer containing flat, 2D pixel data. """
    def __init__(self, n_rows, n_cols, sref, geotrans, data=None, data_type="numpy", io=None, label=None, parent=None):
        """
        Basic constructor of class `RasterLayer`.

        Parameters
        ----------
        n_rows : int
            Number of pixel rows.
        n_cols : int
            Number of pixel columns.
        sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference
            Instance representing the spatial reference of the geometry.
        geotrans : 6-tuple, optional
            GDAL geotransform tuple.
        data : numpy.ndarray or xarray.Dataset, optional
            2D array-like object containing image pixel values.
        data_type : str, optional
            Data type of the returned 2D array-like structure. It can be:
                - 'xarray': loads data as an xarray.DataSet (default)
                - 'numpy': loads data as a numpy.ndarray
        io : pyraster.io.geotiff.GeoTiffFile or pyraster.io.netcdf.NcFile, optional
            Instance of a IO Class that is associated with a file that contains the data.
        label : str or int, optional
            Defines a band or a data variable name.
        parent : RasterLayer, optional
            Parent `RasterLayer` instance.
        """
        super(RasterLayer, self).__init__(n_rows, n_cols, sref, geotrans, data=data, data_type=data_type, io=io,
                                          label=label, parent=parent)

    @property
    def data(self):
        """ numpy.ndarray or xarray.Dataset : Retrieves 2D array in the requested data format. """
        if self._data is not None:
            return self._convert_data_type()

    @data.setter
    def data(self, data):
        """
        Sets internal data to given 2D data.

        Parameters
        ----------
        data : numpy.ndarray or xarray.Dataset
            2D array-like object containing image pixel values.
        """
        self._check_data(data)
        n_data_rows, n_data_cols = convert_data_type(data, "numpy", band=self.label).shape
        n_rows, n_cols = self.raster_geom.n_rows, self.raster_geom.n_cols
        if (n_rows, n_cols) == (n_data_rows, n_data_cols):
            self._data = data
        else:
            wrn_msg = "Data dimension ({}, {}) mismatches raster " \
                      "layer dimension ({}, {}).".format(n_data_rows, n_data_cols, n_rows, n_cols)
            warnings.warn(wrn_msg)

    @classmethod
    def from_filepath(cls, filepath, read=False, read_kwargs=None, io_class=None, io_kwargs=None, **kwargs):
        """
        Creates a `RasterLayer` object from a filepath.

        Parameters
        ----------
        filepath : str
            Full file path to the raster file.
        read : bool, optional
            If true, data is read and assigned to the `RasterData` class.
        read_kwargs : dict, optional
            Keyword arguments for the reading function of the IO class.
        io_class : class, optional
            IO class.
        io_kwargs : dict, optional
            Keyword arguments for IO class initialisation.
        **kwargs
            Keyword arguments for `RasterData` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterLayer
        """

        io_class, _ = cls._io_class_from_filepath(filepath) if io_class is None else io_class

        io_kwargs = io_kwargs if io_kwargs is not None else {}
        io = io_class(filepath, mode='r', **io_kwargs)

        return cls.from_io(io, read=read, read_kwargs=read_kwargs, **kwargs)

    @classmethod
    def from_io(cls, io, read=False, read_kwargs=None, **kwargs):
        """
        Creates a `RasterLayer` object from an IO class instance.

        Parameters
        ----------
        io : GeoTiffFile or NcFile or object
            IO class instance.
        read : bool, optional
            If true, data is read and assigned to the `RasterData` class.
        read_kwargs : dict, optional
            Keyword arguments for the reading function of the IO class.
        **kwargs
            Keyword arguments for `RasterData` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterLayer
        """
        sref = SpatialRef(io.spatialref)
        geotrans = io.geotransform

        if read:
            read_kwargs = read_kwargs if read_kwargs is not None else {}
            data = io.read(**read_kwargs)
            return cls.from_array(sref, geotrans, data, io=io, **kwargs)
        else:
            n_rows, n_cols = io.shape
            return cls(n_rows, n_cols, sref, geotrans, io=io, **kwargs)

    @classmethod
    def from_array(cls, sref, geotrans, data, **kwargs):
        """
        Creates a `RasterData` object from a 2D array-like object.

        Parameters
        ----------
        sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference
            Instance representing the spatial reference of the geometry.
        geotrans : 6-tuple, optional
            GDAL geotransform tuple.
        data : numpy.ndarray or xarray.Dataset, optional
            2D array-like object containing image pixel values.
        **kwargs
            Keyword arguments for `RasterData` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterLayer
        """

        n_rows, n_cols = None, None
        if isinstance(data, np.ndarray):
            n = len(data.shape)
            if n == 2:
                n_rows, n_cols = data.shape
        elif isinstance(data, xr.Dataset):
            n = len(data.dims)
            n_rows = len(data.coords['y'])
            n_cols = len(data.coords['x'])
        else:
            err_msg = "Data type is not supported for this class."
            raise Exception(err_msg)

        if n != 2:
            raise DimensionsMismatch(n, 2)

        return cls(n_rows, n_cols, sref, geotrans, data=data, **kwargs)

    def write(self, filepath, data=None, io_class=None, io_kwargs=None, write_kwargs=None, encode=True,
              encode_kwargs=None):
        """
        Writes 2D array-like data to disk into a target file or into a file associated
        with this object.

        Parameters
        ----------
        filepath : str
            Full file path of the output file.
        data : numpy.ndarray or xarray.Dataset, optional
            2D array-like object containing image pixel values. If None, the `self.data` is used.
        io_class : class, optional
            IO class.
        io_kwargs : dict, optional
            Keyword arguments for IO class initialisation.
        write_kwargs : dict, optional
            Keyword arguments for writing function of IO class.
        encode : bool, optional
            If true, encoding function of `RasterData` class is applied (default is True).
        encode_kwargs : dict, optional
            Keyword arguments for the encoder.
        """

        write_kwargs = write_kwargs if write_kwargs is not None else {}
        io_kwargs = io_kwargs if io_kwargs is not None else {}

        io_class, file_type = self._io_class_from_filepath(filepath) if io_class is None else io_class
        io = io_class(filepath, mode='w', **io_kwargs)

        if file_type == "GeoTIFF":
            data_type = "numpy"
        elif file_type == "NetCDF":
            data_type = "xarray"
        else:
            data_type = self.data_type

        data = data if self._data is None else self._data
        self._check_data(data)
        data = self._convert_data_type(data=data, data_type=data_type)

        if encode:
            write_kwargs.update({"encoder": self.encode})
            write_kwargs.update({"encode_kwargs": encode_kwargs})
        io.write(data, **write_kwargs)
        io.close()

    # TODO: check/enhance all plot arguments
    def plot(self, ax=None, proj=None, extent=None, extent_sref=None, cmap='viridis', add_country_borders=True):
        """
        Plots the data on a map that uses a projection if provided.
        If not, the map projection defaults to the spatial reference
        in which the data are provided. The extent of the data is specified by `sref_extent`.
        If an extent is not provided, it defaults to the bbox of the data's geometry.
        If provided, one can also specify the spatial reference of the extent that is being parsed, otherwise it is
        assumed that the spatial reference of the extent is the same as the spatial reference of the data.

        Parameters
        ----------
        ax :  matplotlib.pyplot.axes
            Pre-defined Matplotlib axis.
        proj :  cartopy.crs.Projection or its subclass, optional
            Projection of the map. The figure will be drawn in
            this spatial reference. If omitted, the spatial reference in which
            the data are present is used.
        extent : 4 tuple, optional
            Extent of the projection (x_min, x_max, y_min, y_max). If omitted, the bbox of the data is used.
        extent_sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference, optional
            Spatial reference of the coordinates.
            Has to be given if the spatial reference is different than the spatial reference of the raster data.
        cmap : matplotlib.colors.Colormap or string, optional
            Colormap for displaying the data (default is 'viridis').
        add_country_borders : bool, optional
            If true, country borders from Natural Earth (1:110m) are added to the map (default is True).

        Returns
        -------
        matplotlib.axes.Axes
        """

        if 'matplotlib' in sys.modules:
            import matplotlib.pyplot as plt
        else:
            err_msg = "Module 'matplotlib' is mandatory for plotting a RasterGeometry object."
            raise ImportError(err_msg)

        if proj is None:
            proj = self.raster_geom.to_cartopy_crs()

        if ax is None:
            fig = plt.figure()
            ax = fig.add_subplot(111, projection=proj)

        ll_x, ll_y, ur_x, ur_y = self.raster_geom.outer_extent
        img_extent = ll_x, ur_x, ll_y, ur_y

        if extent:
            x_min, y_min, x_max, y_max = extent
            if extent_sref:
                x_min, y_min = coordinate_traffo(x_min, y_min, extent_sref, self.raster_geom.sref.osr_sref)
                x_max, y_max = coordinate_traffo(x_max, y_max, extent_sref, self.raster_geom.sref.osr_sref)
            ax.set_xlim([x_min, x_max])
            ax.set_ylim([y_min, y_max])

        if isinstance(cmap, str):
            cmap = cm.get_cmap(cmap)

        # add country borders
        if add_country_borders:
            ax.add_feature(cartopy.feature.BORDERS)

        # plot data
        ax.imshow(self._convert_data_type(data_type="numpy"), extent=img_extent, origin='upper', transform=proj,
                  cmap=cmap)
        ax.set_aspect('equal', 'box')

        return ax

    def _convert_data_type(self, data=None, data_type=None, raster_geom=None):
        """
        Class wrapper for `convert_data_type` function.

        Parameters
        ----------
        data : numpy.ndarray or xarray.Dataset, optional
            2D array-like object containing image pixel values.
        data_type : str, optional
            Data type of the returned array-like structure (default is None -> class variable `data_type` is used).
            It can be:
                - 'xarray': converts data to an xarray.Dataset
                - 'numpy': converts data to a numpy.ndarray
        raster_geom : `RasterGeometry`, optional
            Geometry in synergy with `data`. Needed to select the coordinate values along the data axis.

        Returns
        -------
        numpy.ndarray or xarray.Dataset, optional
            2D array-like object containing image pixel values.
        """

        data = self._data if data is None else data
        data_type = self.data_type if data_type is None else data_type
        raster_geom = self.raster_geom if raster_geom is None else raster_geom

        coords = (raster_geom.y_coords, raster_geom.x_coords)

        return convert_data_type(data, *coords, data_type=data_type, band=self.label)

    @staticmethod
    def _check_data(data):
        """
        Checks array type and structure of data.

        Parameters
        ----------
        data : numpy.ndarray or xarray.Dataset, optional
            2D array-like object containing image pixel values.

        Returns
        -------
        bool
            If true, the given data fulfills all requirements for a `RasterLayer` object.
        """

        if data is not None:
            if isinstance(data, np.ndarray):
                n = len(data.shape)
            elif isinstance(data, xr.Dataset):
                n = len(data.dims)
            else:
                err_msg = "Data type is not supported for this class."
                raise Exception(err_msg)

            if n != 2:
                err_msg = "Data has {} dimensions, but 2 dimensions are required.".format(n)
                raise Exception(err_msg)
            return True
        else:
            return False

    @staticmethod
    def _io_class_from_filepath(filepath):
        """
        Selects an IO class depending on the filepath/file ending.

        Parameters
        ----------
        filepath : str
            Full file path of the output file.

        Returns
        -------
        io_class : class
            IO class.
        file_type : str
            Type of file: can be "GeoTIFF" or "NetCDF".
        """

        tiff_ext = ('.tiff', '.tif', '.geotiff')
        netcdf_ext = ('.nc', '.netcdf', '.ncdf')

        # determine IO class
        file_ext = os.path.splitext(filepath)[1].lower()
        if file_ext in tiff_ext:
            io_class = GeoTiffFile
            file_type = "GeoTIFF"
        elif file_ext in netcdf_ext:
            io_class = NcFile
            file_type = "NetCDF"
        else:
            raise IOError('File format not supported.')

        return io_class, file_type

    def __getitem__(self, item):
        """
        Handles indexing of a raster layer object,
        which is herein defined as a 2D spatial indexing via x and y coordinates.

        Parameters
        ----------
        item : 2-tuple
            Tuple containing coordinate slices (e.g., (10:100,20:200)) or coordinate values.

        Returns
        -------
        RasterLayer
            Raster layer defined by the intersection.
        """

        intsct_raster_geom = self.raster_geom[item]
        return self.crop(intsct_raster_geom, inplace=False)


class RasterStack(RasterData):
    """ Raster data class for multiple congruent raster layers containing 3D pixel data. """
    def __init__(self, raster_layers, data=None, data_type="numpy", io=None, label=None,
                 parent=None):
        """
        Basic constructor of class `RasterStack`.

        Parameters
        ----------
        raster_layers : pandas.Series or list
            Sequence of raster layer objects defining a congruent raster stack.
        data : numpy.ndarray or xarray.Dataset, optional
            3D array-like object containing image pixel values.
        data_type : str, optional
            Data type of the returned array-like structure. It can be:
                - 'xarray': loads data as an xarray.DataSet
                - 'numpy': loads data as a numpy.ndarray (default)
        io : pyraster.io.geotiff.GeoTiffFile or pyraster.io.netcdf.NcFile, optional
            Instance of a IO Class that is associated with a file that contains the data.
        label : str or int, optional
            Defines a band or a data variable name.
        parent : geospade.definition.RasterGeometry, optional
            Parent `RasterGeometry` instance.
        """

        # doing some checks on the given raster layers
        if isinstance(raster_layers, pd.Series):
            pass
        elif isinstance(raster_layers, list):
            raster_layers = pd.Series(raster_layers)
        else:
            err_msg = "'raster_layers' must either be a list or a Pandas Series."
            raise Exception(err_msg)

        base_raster_geom = raster_layers[raster_layers.notna()][0].raster_geom
        if not all([base_raster_geom == raster_layer.raster_geom
                    for raster_layer in raster_layers[raster_layers.notna()].values]):
            err_msg = "The raster layers are not congruent."
            raise Exception(err_msg)

        super(RasterStack, self).__init__(base_raster_geom.n_rows, base_raster_geom.n_cols, base_raster_geom.sref,
                                          base_raster_geom.geotrans, data=data, data_type=data_type, io=io,
                                          label=label, parent=parent)

        self.raster_layers = raster_layers

    @property
    def data(self):
        """ numpy.ndarray or xarray.Dataset : Retrieves array in the requested data format. """
        if self._data is not None:
            return self._convert_data_type()

    @data.setter
    def data(self, data):
        """
        Sets internal data to given 3D data.

        Parameters
        ----------
        data : numpy.ndarray or xarray.Dataset
            3D array-like object containing image pixel values.
        """
        self._check_data(data)
        n_data_layers, n_data_rows, n_data_cols = convert_data_type(data, "numpy", band=self.label).shape

        n_layers, n_rows, n_cols = len(self.raster_layers), self.raster_geom.n_rows, self.raster_geom.n_cols
        if (n_layers, n_rows, n_cols) == (n_data_layers, n_data_rows, n_data_cols):
            self._data = data
        else:
            wrn_msg = "Data dimension ({}, {}, {}) mismatches " \
                      "Raster stack dimension ({}, {}, {}).".format(n_data_layers, n_data_rows, n_data_cols,
                                                                    n_layers, n_rows, n_cols)
            warnings.warn(wrn_msg)

    @classmethod
    def from_array(cls, sref, geotrans, data, layer_ids=None, **kwargs):
        """
        Creates a `RasterStack` object from a 3D array-like object.

        Parameters
        ----------
        sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference
            Instance representing the spatial reference of the geometry.
        geotrans : 6-tuple, optional
            GDAL geotransform tuple.
        data : numpy.ndarray or xarray.Dataset, optional
            3D array-like object containing image pixel values.
        layer_ids : list, optional
            Layer IDs/labels referring to first dimension of `data`.
        **kwargs
            Keyword arguments for `RasterData` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterStack
        """

        n_layers, n_rows, n_cols = None, None, None
        if isinstance(data, np.ndarray):
            n = len(data.shape)
            if n == 3:
                n_layers, n_rows, n_cols = data.shape
        elif isinstance(data, xr.Dataset):
            n = len(data.dims)
            if n == 3:
                dim_names = list(data.coords.keys())
                n_layers = len(data.coords[dim_names[0]])
                n_rows = len(data.coords[dim_names[1]])
                n_cols = len(data.coords[dim_names[2]])
        else:
            err_msg = "Data type is not supported for this class."
            raise Exception(err_msg)

        if n != 3:
            raise DimensionsMismatch(n, 3)

        if layer_ids is not None:
            n_layer_ids = len(layer_ids)
            if n_layer_ids != n_layers:
                err_msg = "Number of given layer IDs ({}) does not match " \
                          "with number of data layers ({}).".format(n_layer_ids, n_layers)
                raise Exception(err_msg)

        raster_layers = pd.Series([RasterLayer(n_rows, n_cols, sref, geotrans)] * n_layers, index=layer_ids)
        return cls(raster_layers, data=data, **kwargs)

    @classmethod
    def from_filepath(cls, filepaths, read=False, read_kwargs=None, io_class=None, io_kwargs=None, **kwargs):
        """
        Creates a `RasterStack` object from a list of filepaths.

        Parameters
        ----------
        filepaths : str or list of str
            Full file path(s) to the raster file(s).
        read : bool, optional
            If true, data is read and assigned to the `RasterData` class.
        read_kwargs : dict, optional
            Keyword arguments for the reading function of the IO class.
        io_class : class, optional
            IO class.
        io_kwargs : dict, optional
            Keyword arguments for IO class initialisation.
        **kwargs
            Keyword arguments for `RasterStack` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterStack
        """

        if not isinstance(filepaths, list):
            filepaths = [filepaths]

        file_types = set([cls._io_class_from_filepath(filepath)[1] for filepath in filepaths if filepath is not None])
        if len(file_types) > 1:
            err_msg = "Only one file type is allowed."
            raise Exception(err_msg)

        filepath_stack = pd.DataFrame({'filepath': filepaths})
        filepath_ref = filepath_stack[filepath_stack.notna()]['filepath'][0]
        io_class, _ = cls._io_class_from_filepath(filepath_ref) if io_class is None else io_class
        io_kwargs = io_kwargs if io_kwargs is not None else {}

        io = io_class(file_ts=filepath_stack, mode='r', **io_kwargs)

        return cls.from_io(io, read=read, read_kwargs=read_kwargs, **kwargs)

    @classmethod
    def from_io(cls, io, read=False, read_kwargs=None, **kwargs):
        """
        Creates a `RasterStack` object from an IO class instance.

        Parameters
        ----------
        io : GeoTiffFile or NcFile or object
            IO class instance.
        read : bool, optional
            If true, data is read and assigned to the `RasterData` class.
        read_kwargs : dict, optional
            Keyword arguments for the reading function of the IO class.
        **kwargs
            Keyword arguments for `RasterData` constructor, i.e. `data_type`, `io`, `label` or `parent`.

        Returns
        -------
        RasterStack
        """

        sref = SpatialRef(io.spatialref)
        geotrans = io.geotransform

        if read:
            read_kwargs = read_kwargs if read_kwargs is not None else {}
            data, labels = io.read(**read_kwargs)
            return cls.from_array(sref, geotrans, data, layer_ids=labels, io=io, **kwargs)
        else:
            n_layers, n_rows, n_cols = io.shape
            raster_layers = pd.Series([RasterLayer(n_rows, n_cols, sref, geotrans)] * n_layers, index=io.layers)
            return cls(raster_layers, io=io, **kwargs)

    def get_layer_data(self, layer_ids, data_type="numpy"):
        """
        Returns 2D array-like data in a list corresponding to the given list of layer ID's.

        Parameters
        ----------
        layer_ids : object
            Layer ID's representing the labels of the stack dimension.
        data_type : str, optional
            Data type of the returned 2D array-like structure. It can be:
                - 'xarray': loads data as an xarray.DataSet
                - 'numpy': loads data as a numpy.ndarray (default)

        Returns
        -------
        List of xarray.DataSets or numpy.ndarrays
        """
        if not isinstance(layer_ids, list):
            layer_ids = [layer_ids]

        layer_data = []
        all_layer_ids = list(self.raster_layers.index)
        for layer_id in layer_ids:
            idx = all_layer_ids.index(layer_id)
            if self._data is None:
                raster_layer = self.raster_layers.loc[layer_id]
                raster_layer.load(data_type=data_type, inplace=True)
                layer_data.append(raster_layer.data)
            else:
                px_slices = (slice(0, self.raster_geom.n_rows), slice(0, self.raster_geom.n_cols))
                layer_slice = [slice(idx, idx+1)]
                raster_stack = self._load_array(px_slices, slices=layer_slice, data_type=data_type, inplace=False)
                layer_data.append(raster_stack.data)

        return layer_data

    def crop(self, geom=None, sref=None, layer_ids=None, apply_mask=False, buffer=0, inplace=False):
        """
        Crops the loaded data by a geometry and/or layer ID's. In addition, a mask can be applied with a certain buffer.
        `inplace` determines, whether a new object is returned or the cropping happens on the object instance.

        Parameters
        ----------
        geom : geospade.definition.RasterGeometry or ogr.Geometry or shapely.geometry or list or tuple, optional
            Geometry defining the data extent of interest.
        sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference, optional
            Spatial reference of the geometry.
            Has to be given if the spatial reference is different than the spatial reference of the raster data.
            Note: `sref` is used in the decorator `_any_geom2ogr_geom`.
        layer_ids : object or list of objects
            Layer ID's representing the labels of the stack dimension (used for selecting a subset).
        apply_mask : bool, optional
            If true, a mask is applied for data points being not inside the given geometry (default is False).
        buffer : int, optional
            Pixel buffer for crop geometry (default is 0).
        inplace : bool, optional
            If true, the current instance will be modified.
            If false, a new `RasterData` instance will be created (default).

        Returns
        -------
        RasterStack
            `RasterStack` object only containing data within the intersection.
            If the `RasterStack` and the given geometry do not intersect, None is returned.
        """
        layer_slice = None
        if layer_ids is not None:
            if not isinstance(layer_ids, list):
                layer_ids = [layer_ids]
            all_layer_ids = list(self.raster_layers.index)
            layer_idxs = [all_layer_ids.index(layer_id) for layer_id in layer_ids]
            layer_slice = [layer_idxs]

        if geom is None:
            px_slices = (slice(0, self.raster_geom.n_rows), slice(0, self.raster_geom.n_cols))
            return self._load_array(px_slices, slices=layer_slice, inplace=inplace)
        else:
            return super().crop(geom, sref=sref, slices=layer_slice, apply_mask=apply_mask, buffer=buffer,
                                inplace=inplace)

    def write(self, filepaths, layer_ids=None, stack=True, encode=True, encode_kwargs=None, **kwargs):
        """
        Writes data to disk into a target file or into a file associated
        with this object.

        Parameters
        ----------
        filepaths : str, list of str
            Full file paths of the output file.
        layer_ids : object or list of objects
            Layer ID's representing the labels of the stack dimension (used for selecting a subset).
        stack : bool, optional
            If true, than the whole stack is written to one file.
            If false, then each layer is written to a separate file.
        encode : bool, optional
            If true, encoding function of `RasterData` class is applied (default is True).
        encode_kwargs : dict, optional
            Keyword arguments for the encoder.
        **kwargs : Keyword arguments for `write_stack` or `write_layers`, i.e. `io_class`, `io_kwargs`, `write_kwargs`.
        """
        if not isinstance(filepaths, list):
            filepaths = [filepaths]

        if stack:
            if len(filepaths) > 1:
                err_msg = "Only one filepath is allowed when a stack is written to disk."
                raise Exception(err_msg)
            filepath = filepaths[0]
            self.write_stack(filepath, layer_ids=layer_ids, encode=encode, encode_kwargs=encode_kwargs, **kwargs)
        else:
            self.write_layers(filepaths, layer_ids=layer_ids, encode=encode, encode_kwargs=encode_kwargs, **kwargs)

    def write_stack(self, filepath, layer_ids=None, encode=True, encode_kwargs=None, io_class=None, io_kwargs=None,
                    write_kwargs=None):
        """
        Writes all/a subset raster stack data to one file on disk. The subset selection can be done by specifying
        a list of labels/layer ID's in `layer_ids`.

        Parameters
        ----------
        filepath : str
            Full file path of the output file.
        layer_ids : object or list of objects
            Layer ID's representing the labels of the stack dimension (used for selecting a subset).
        encode : bool, optional
            If true, encoding function of `RasterData` class is applied (default is True).
        encode_kwargs : dict, optional
            Keyword arguments for the encoder.
        io_class : class, optional
            IO class.
        io_kwargs : dict, optional
            Keyword arguments for IO class initialisation.
        write_kwargs : dict, optional
            Keyword arguments for writing function of IO class.
        """

        write_kwargs = write_kwargs if write_kwargs is not None else {}
        io_kwargs = io_kwargs if io_kwargs is not None else {}

        io_class, file_type = self._io_class_from_filepath(filepath) if io_class is None else io_class
        raster_layers = self.raster_layers.loc[layer_ids]
        filepath_stack = pd.DataFrame({'filepath': [None if raster_layer.io is None else raster_layer.filepath
                                                    for raster_layer in raster_layers]})
        io = io_class(file_ts=filepath_stack, mode='w', **io_kwargs)

        if file_type == "GeoTIFF":
            data_type = "numpy"
        elif file_type == "NetCDF":
            data_type = "xarray"
        else:
            data_type = self.data_type
        data = self._convert_data_type(data_type=data_type)

        if encode:
            write_kwargs.update({"encoder": self.encode})
            write_kwargs.update({"encode_kwargs": encode_kwargs})
        io.write(data, **write_kwargs)
        io.close()

    def write_layers(self, filepaths, layer_ids=None, io_class=None, io_kwargs=None, write_kwargs=None,
                     encode=True, encode_kwargs=None):
        """
        Writes each layer of all/a subset raster stack data to a file on disk. The subset selection can be done by
        specifying a list of labels/layer ID's in `layer_ids`.

        Parameters
        ----------
        filepaths : list of str
            Full file paths of the output file.
        layer_ids : object or list of objects
            Layer ID's representing the labels of the stack dimension (used for selecting a subset).
        encode : bool, optional
            If true, encoding function of `RasterData` class is applied (default is True).
        encode_kwargs : dict, optional
            Keyword arguments for the encoder.
        io_class : class, optional
            IO class.
        io_kwargs : dict, optional
            Keyword arguments for IO class initialisation.
        write_kwargs : dict, optional
            Keyword arguments for writing function of IO class.
        """
        write_kwargs = write_kwargs if write_kwargs is not None else {}
        io_kwargs = io_kwargs if io_kwargs is not None else {}

        if len(filepaths) != len(layer_ids):
            err_msg = "Number of given filepaths ({}) does not match number of given indizes ({})."
            err_msg = err_msg.format(len(filepaths), len(layer_ids))
            raise Exception(err_msg)

        all_layer_ids = list(self.raster_layers.index)
        for i, filepath in enumerate(filepaths):
            layer_id = layer_ids[i]
            if layer_id not in all_layer_ids:
                err_msg = "Layer ID {} is not available.".format(layer_ids[i])
                raise IndexError(err_msg)

            raster_layer = self.raster_layers.loc[layer_id]
            data = self.get_layer_data(layer_id, data_type=self.data_type)[0]
            raster_layer.write(filepath, data=data, io_class=io_class, io_kwargs=io_kwargs, write_kwargs=write_kwargs,
                               encode=encode, encode_kwargs=encode_kwargs)

    # TODO: add more functionalities and options
    def plot(self, layer_id=None, ax=None, proj=None, proj_extent=None, extent_sref=None, cmap='viridis',
             interactive=False, add_country_borders=True):
        """
        Plots the data on a map that uses a projection if provided.
        If not, the map projection defaults to the spatial reference
        in which the data are provided. The extent of the data is specified by `extent`.
        If an extent is not provided, it defaults to the bbox of the data's geometry.
        If provided, one can also specify the spatial reference of the extent that is being parsed, otherwise it is
        assumed that the spatial reference of the extent is the same as the spatial reference of the data.

        Parameters
        ----------
        ax :  matplotlib.pyplot.axes
            Pre-defined Matplotlib axis.
        layer_id : object
            Layer ID representing one labels of the stack dimension.
        proj :  cartopy.crs.Projection or its subclass, optional
            Projection of the map. The figure will be drawn in
            this spatial reference. If omitted, the spatial reference in which
            the data are present is used.
        proj_extent : 4 tuple, optional
            Extent of the projection (x_min, x_max, y_min, y_max). If omitted, the bbox of the data is used.
        extent_sref : geospade.spatial_ref.SpatialRef or osr.SpatialReference, optional
            Spatial reference of the coordinates.
            Has to be given if the spatial reference is different than the spatial reference of the raster data.
        cmap : matplotlib.colors.Colormap or string, optional
            Colormap for displaying the data (default is 'viridis').
        interactive : bool, optional
            If true, one can interactively switch between different raster stack layers in the plot.
            If false, only the initial raster stack layer will be shown (default is False).
        add_country_borders : bool, optional
            If true, country borders from Natural Earth (1:110m) are added to the map (default is True).

        Returns
        -------
        matplotlib.axes.Axes, RasterStackSlider
        """

        # loading Matplotlib module if it is available
        if 'matplotlib' in sys.modules:
            import matplotlib.pyplot as plt
        else:
            err_msg = "Module 'matplotlib' is mandatory for plotting a RasterGeometry object."
            raise ImportError(err_msg)

        if layer_id is None:
            layer_id = list(self.raster_layers.index)[0]  # take the first layer as a default value

        # create new figure if it is necessary
        if ax is None:
            fig = plt.figure()
            ax = fig.add_subplot(111, projection=proj)

        # get projection definition from raster geometry
        if proj is None:
            proj = self.raster_geom.to_cartopy_crs()

        # limit axes to the given extent in the projection
        if proj_extent:
            x_min, y_min, x_max, y_max = proj_extent
            if extent_sref:
                x_min, y_min = coordinate_traffo(x_min, y_min, extent_sref, self.raster_geom.sref.osr_sref)
                x_max, y_max = coordinate_traffo(x_max, y_max, extent_sref, self.raster_geom.sref.osr_sref)
            ax.set_xlim([x_min, x_max])
            ax.set_ylim([y_min, y_max])

        # get colourmap
        if isinstance(cmap, str):
            cmap = cm.get_cmap(cmap)

        if add_country_borders:
            ax.coastlines()
            ax.add_feature(cartopy.feature.BORDERS)

        # get image extent in the projection of the raster geometry
        ll_x, ll_y, ur_x, ur_y = self.raster_geom.outer_extent
        img_extent = ll_x, ur_x, ll_y, ur_y

        # plot image data
        layer_data = self.get_layer_data(layer_id)[0]
        # -2 because pixel dimensions (row, col) are the last two
        slices = [0]*(len(layer_data.shape)-2) + [slice(None)]*2
        img = ax.imshow(layer_data[tuple(slices)], extent=img_extent, origin='upper', transform=proj, cmap=cmap)

        ax.set_aspect('equal', 'box')

        # interactive plot settings
        slider = None
        if interactive:
            canvas_bounds = ax.get_position().bounds
            ax_slider = plt.axes([canvas_bounds[0], 0.05, canvas_bounds[2], 0.03], facecolor='lightgoldenrodyellow')
            layer_idx = list(self.raster_layers.index).index(layer_id)
            slider = RasterStackSlider(self, ax_slider, valinit=layer_idx, valstep=1)

            def update(idx):
                layer_id = list(self.raster_layers.index)[int(idx)]
                layer_data = self.get_layer_data(layer_id)[0]
                # -2 because pixel dimensions (row, col) are the last two
                slices = [0] * (len(layer_data.shape) - 2) + [slice(None)] * 2
                img.set_data(layer_data[tuple(slices)])
                fig.canvas.draw_idle()

            slider.on_changed(update)

        plt.show()
        return ax, slider

    def _convert_data_type(self, data=None, data_type=None, raster_geom=None):
        """
        Class wrapper for `convert_data_type` function.

        Parameters
        ----------
        data : numpy.ndarray or xarray.Dataset, optional
            3D array-like object containing image pixel values.
        data_type : str, optional
            Data type of the returned array-like structure (default is None -> class variable `data_type` is used).
            It can be:
                - 'xarray': converts data to an xarray.Dataset
                - 'numpy': converts data to a numpy.ndarray
        raster_geom : `RasterGeometry`, optional
            Geometry in synergy with `data`. Needed to select the coordinate values along the data axis.

        Returns
        -------
        numpy.ndarray or xarray.Dataset
            3D array-like object containing image pixel values.
        """

        data = self._data if data is None else data
        data_type = self.data_type if data_type is None else data_type
        raster_geom = self.raster_geom if raster_geom is None else raster_geom

        coords = (list(self.raster_layers.index), raster_geom.y_coords, raster_geom.x_coords)

        return convert_data_type(data, *coords, data_type=data_type, band=self.label)

    @staticmethod
    def _check_data(data):
        """
        Checks array type and structure of data.

        Parameters
        ----------
        data : numpy.ndarray or xarray.Dataset, optional
            3D array-like object containing image pixel values.

        Returns
        -------
        bool
            If true, the given data fulfills all requirements for a `RasterStack` object.
        """

        if data is not None:
            if isinstance(data, np.ndarray):
                n = len(data.shape)
            elif isinstance(data, xr.Dataset):
                n = len(data.dims)
            else:
                err_msg = "Data type is not supported for this class."
                raise Exception(err_msg)

            if n != 3:
                err_msg = "Data has {} dimensions, but 3 dimensions are required."
                raise Exception(err_msg.format(n))
            return True
        else:
            return False

    @staticmethod
    def _io_class_from_filepath(filepath):
        """
        Selects an IO class depending on the filepath/file ending.

        Parameters
        ----------
        filepath : str
            Full file path of the output file.

        Returns
        -------
        io_class : class
            IO class.
        file_type : str
            Type of file: can be "GeoTiffRasterStack" or "NcRasterStack".
        """

        tiff_ext = ('.tiff', '.tif', '.geotiff')
        netcdf_ext = ('.nc', '.netcdf', '.ncdf')

        # determine IO class
        file_ext = os.path.splitext(filepath)[1].lower()
        if file_ext in tiff_ext:
            io_class = GeoTiffRasterStack
            file_type = "GeoTIFF"
        elif file_ext in netcdf_ext:
            io_class = NcRasterStack
            file_type = "NetCDF"
        else:
            raise IOError('File format not supported.')

        return io_class, file_type

    def __len__(self):
        """ Returns amount of raster stack layers. """
        return len(self.raster_layers)

    def __getitem__(self, item):
        """
        Handles indexing of a raster layer object,
        which is herein defined as a 3D spatial indexing via labels/layer ID's, x and y coordinates.

        Parameters
        ----------
        item : 3-tuple
            Tuple containing coordinate slices (e.g., ("B2", 10:100, 20:200)) or coordinate values.

        Returns
        -------
        RasterStack
            Raster stack defined by the intersection.
        """
        if not isinstance(item, tuple) or (isinstance(item, tuple) and len(item) != 3):
            raise ValueError('Index must be a tuple containing the layer id, x and y coordinates.')
        else:
            if isinstance(item[0], slice):
                start_layer_id = item[0].start
                stop_layer_id = item[0].stop
                all_layer_ids = list(self.raster_layers.index)
                if start_layer_id is not None and stop_layer_id is not None:
                    start_layer_idx = all_layer_ids.index(start_layer_id)
                    stop_layer_idx = all_layer_ids.index(stop_layer_id)
                    if stop_layer_id < start_layer_id:
                        err_msg = "First index is larger than second index."
                        raise Exception(err_msg)
                    # +1 because last layer id needs to be included
                    layer_ids = all_layer_ids[start_layer_idx:(stop_layer_idx+1)]
                elif start_layer_id is not None:
                    start_layer_idx = all_layer_ids.index(start_layer_id)
                    layer_ids = all_layer_ids[start_layer_idx:]
                elif stop_layer_id is not None:
                    stop_layer_idx = all_layer_ids.index(stop_layer_id)
                    layer_ids = all_layer_ids[:(stop_layer_idx+1)]
                else:
                    layer_ids = all_layer_ids
            else:
                layer_ids = [item[0]]

        intsct_raster_geom = self.raster_geom[item[1:]]

        return self.crop(geom=intsct_raster_geom, layer_ids=layer_ids, inplace=False)


class RasterMosaic(object):

    def __init__(self, raster_stacks, grid=None, aggregator=None):

        self.aggregator = aggregator
        self.raster_stacks = raster_stacks

        raster_geoms = [raster_stack.geometry for raster_stack in self.raster_stacks
                        if raster_stack.geometry is not None]

        if grid is None:
            self.grid = RasterGrid(raster_geoms)
        else:
            self.grid = grid

        self.geometry = RasterGeometry.get_common_geometry(raster_geoms)

    @classmethod
    def from_df(cls, arg, **kwargs):
        pass

    @classmethod
    def from_list(cls, arg, **kwargs):
        arg_dict = dict()
        for i, layer in enumerate(arg):
            arg_dict[i] = layer

        return cls.from_dict(arg_dict, **kwargs)

    @classmethod
    def from_dict(cls, arg, **kwargs):
        structure = RasterMosaic._dict2structure(arg)
        raster_stacks = cls._build_raster_stacks(structure)

        return cls(raster_stacks)

    @staticmethod
    def _dict2structure(struct_dict):
        structure = dict()
        structure["raster_data"] = []
        structure["spatial_id"] = []
        structure["layer_id"] = []
        geoms = []
        for layer_id, layer in struct_dict.items():
            for elem in layer:
                structure['layer_id'].append(layer_id)
                if isinstance(elem, RasterData):
                    rd = elem
                elif isinstance(elem, str) and os.path.exists(elem):
                    rd = RasterData.from_file(elem, mode=None)
                else:
                    raise Exception("Data type '{}' is not understood.".format(type()))

                structure['raster_data'].append(rd)
                if rd.geometry.id is not None:
                    structure['spatial_id'].append(rd.geometry.id)
                else:
                    spatial_id = None
                    for i, geom in enumerate(geoms):
                        if geom == rd.geometry:
                            spatial_id = i
                            break
                    if spatial_id is None:
                        spatial_id = len(geoms)
                        geoms.append(rd.geometry)
                    structure['spatial_id'].append(spatial_id)

        return pd.DataFrame(structure)

    @staticmethod
    def _build_raster_stacks(structure):
        struct_groups = structure.groupby(by="spatial_id")
        raster_stacks = {'raster_stack': []}
        spatial_ids = []
        for struct_group in struct_groups:
            raster_datas = struct_group['raster_data']
            raster_datas.set_index(struct_group['layer_id'], inplace=True)
            raster_stacks['raster_stack'].append(RasterStack(raster_datas))
            spatial_ids.append(struct_group['spatial_id'])

        return pd.Series(raster_stacks, index=spatial_ids)

    @_any_geom2ogr_geom
    def read_by_geom(self, geom, osr_sref=None, band=1, dtype='numpy', **kwargs):
        raster_grid = self.grid.crop(geom)
        geometry = self.geometry.crop(geom)
        spatial_ids = raster_grid.geom_ids
        raster_stacks = [raster_stack.load_by_geom(geom, osr_sref=osr_sref, dtype=dtype, band=band, inplace=False, **kwargs)
                         for raster_stack in self.raster_stacks[spatial_ids]]

        return RasterStack.from_others(raster_stacks, dtype=dtype, geometry=geometry)

    def read_by_coord(self, x, y, osr_sref=None, band=1, dtype='numpy', **kwargs):
        point = Point((x, y))
        return self.read_by_geom(point, osr_sref=osr_sref, band=band, dtype=dtype, **kwargs)

    def read(self, col, row, col_size=1, row_size=1, band=1, dtype="numpy", **kwargs):
        if col_size == 1 and row_size == 1:
            x, y = ij2xy(col, row, self.geometry.gt)
            return self.read_by_coord(x, y, band=band, dtype=dtype, **kwargs)
        else:
            max_col = col + col_size
            max_row = row + row_size
            min_x, min_y = ij2xy(col, max_row, self.geometry.gt)
            max_x, max_y = ij2xy(max_col, row, self.geometry.gt)
            bbox = [(min_x, min_y), (max_x, max_y)]
            return self.read_by_geom(bbox, band=band, dtype=dtype, **kwargs)


if __name__ == '__main__':
    pass





