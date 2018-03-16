import os

from oggm import cfg, workflow
from oggm.core.gis import _check_geometry
import geopandas as gpd
import salem
import time
import multiprocessing as mp
import numpy as np
import pandas as pd
import zipfile
import warnings
import copy

warnings.simplefilter(action='ignore', category=FutureWarning)


def run_div(gdir):
    input_shp = gdir.get_filepath('outlines')
    input_dem = gdir.get_filepath('dem')

    script = os.path.join(project, 'partitioning/examples/run_divides.py')
    os.system(python + ' ' + script + ' ' + input_shp + ' ' + input_dem)


def _merge_sliver(gpd_obj, polygon):
    """merge sliver polygon to the glacier with the longest shared boundary.
    If polygon does not touch the glaciers, False will be returned.

    Parameters
    ----------
    gpd_obj : gpd.GeoDataFrame
        contains the geometry of each glacier
    polygon : shapely.geometry.Polygon instance
        sliver polygon, which should be merged

    Returns
    -------
    new gpd.GeoDataFrame,
    bool
    """

    intersection_array = gpd_obj.intersection(polygon.boundary).length

    if np.max(intersection_array) != 0:
        max_b = np.argmax(intersection_array)
        poly = gpd_obj.loc[max_b, 'geometry'].simplify(0.01).buffer(0)
        geom = poly.union(polygon.buffer(0)).buffer(0)
        if geom.type is not 'Polygon':
            geom = geom.buffer(-0.01).buffer(0.01)
        gpd_obj.set_value(max_b, 'geometry', geom)
        merged = True
    # sliver does not touch glacier at the moment. Try again in the end
    else:
        merged = False
    return [gpd_obj, merged]


def _filter_divides(gpd_obj, filter_area, filter_alt_range,
                    filter_perc_alt_range):
    """ filter divides

    Parameters
    ----------
    gpd_obj                 : gpd.GeoDataFrame
    filter_area             : bool
                              (True: keep a divide only if it's area is not
                               smaller than 2% of the largest divide)
    filter_alt_range        : bool
                              (True: keep a divide only if the absolute
                               altitude range of the divide is larger than 100m
    filter_perc_alt_range   : bool
                              (True: keep a divide only if the altitude range
                               of the divide is larger than 10% of the glaciers
                               total altitude range

    Returns
    -------
    filtered gpd.GeoDataFrame object
    """

    # initialise nokeep
    nokeep = pd.Series(np.zeros(len(gpd_obj), dtype=np.bool))
    if filter_area is True:
        nokeep = nokeep | (gpd_obj['Perc_Area'] < 0.02)
    if filter_alt_range is True:
        nokeep = nokeep | (gpd_obj['Alt_Range'] < 100)
    if filter_perc_alt_range is True:
        nokeep = nokeep | (gpd_obj['Perc_Alt_R'] < 0.1)

    gpd_obj['keep'] = ~nokeep

    if np.sum(gpd_obj['keep']) in [0, 1]:
        # Nothing to do! The divide should be ignored
        return gpd_obj, False

    while not gpd_obj['keep'].all():
        geom = gpd_obj.loc[~gpd_obj['keep']].iloc[0]
        gpd_obj = gpd_obj.drop(geom.name)
        if geom.geometry.type == 'Polygon':
            gpd_obj, bool = _merge_sliver(gpd_obj, geom.geometry)
        else:
            for geo in geom.geometry:
                gpd_obj, bool = _merge_sliver(gpd_obj, geo)
    return gpd_obj, True


def remove_multipolygon(gpd_obj):
    '''
    try to change mulitpolygons to polygons
    Parameters
    ----------
    gpd_obj: GeoDataFrame containing the some MultiPolygons

    Returns  GeoDataFrame
    -------

    '''
    original = copy.deepcopy(gpd_obj)
    repair = gpd_obj[gpd_obj.type != 'Polygon']
    try:
        for i, geo in zip(repair.index, repair.geometry):
            area = np.array([g.area for g in geo])
            gpd_obj.set_value(i, 'geometry', geo[np.argmax(area)])
            indices = list(range(len(geo)))
            del indices[np.argmax(area)]
            for i in indices:
                gpd_obj, keep = _merge_sliver(gpd_obj, geo[i])
        return gpd_obj
    except:
        return original


def check_for_islands(gpd_obj, outline):
    '''
    check if some divides did not intersects with the exterior of the outline
    and removes them

    Parameters
    ----------
    gpd_obj : GeoDataFrame
    outline : outline of the glaciers

    Returns : GeoDataFrame without islands
    -------

    '''

    original = copy.deepcopy(gpd_obj)
    try:
        intersects = gpd_obj.intersects(outline.exterior.loc[0])
        to_merge = gpd_obj[~intersects].geometry
        gpd_obj = gpd_obj[intersects].reset_index()
        for geom in to_merge:
            gpd_obj, bool = _merge_sliver(gpd_obj, geom)
        return gpd_obj
    except:
        return original


def postprocessing(rgidf, gdir, input_shp, filter):
    outline = gpd.read_file(input_shp)

    index = rgidf[rgidf['RGIId'] == gdir.rgi_id].index
    rgidf.loc[index, 'OGGM_Area'] = [outline.area / 10 ** 6]

    if os.path.exists(
            os.path.join(os.path.dirname(input_shp), 'divides.shp')):
        glaciers = gpd.read_file(
            os.path.join(os.path.dirname(input_shp), 'divides.shp'))
    else:
        return rgidf

    glaciers = remove_multipolygon(glaciers)
    glaciers = check_for_islands(glaciers, outline)
    glaciers = glaciers.to_crs(rgidf.crs)

    filter_area = False
    filter_alt_range = False
    filter_perc_alt_range = False
    if filter == 'alt_filter':
        filter_alt_range = True
        filter_perc_alt_range = True
    elif filter == 'all_filter':
        filter_area = True
        filter_alt_range = True
        filter_perc_alt_range = True

    try:
        glaciers, keep = _filter_divides(glaciers, filter_area,
                                         filter_alt_range,
                                         filter_perc_alt_range)
    except:
        keep = True
        rgidf.loc[index, 'remarks'] = ['no filter was used (error in merging)']

    divide = copy.deepcopy(outline)

    for i in range(len(glaciers)-1):
        divide = divide.append(outline, ignore_index=True)

    divide['Area'] = ""
    divide['remarks'] = " "
    if 'level_0' in glaciers.columns:
        glaciers = glaciers.drop('level_0', axis=1)
    glaciers = glaciers.reset_index()
    for i in glaciers.index:
        divide.loc[i]['geometry'] = glaciers.loc[i]['geometry']
    geo_is_ok = []
    new_geo = []

    # check geometry
    for g, a in zip(divide.geometry, glaciers.Area):

        if a < 0.01:
            geo_is_ok.append(False)
            continue

        try:
            new_geo.append(_check_geometry(g))
            geo_is_ok.append(True)
        except:
            geo_is_ok.append(False)

    failed = divide.iloc[np.invert(geo_is_ok)]
    for i in failed.index:
        failed.loc[i, 'remarks'] = 'check_geometry failed'

    divide = divide.iloc[geo_is_ok]
    divide = divide.append(failed, ignore_index=False)

    # change RGIId
    new_id = [divide.loc[i, 'RGIId'] + '_d' + str(i + 1).zfill(2) for i
              in range(len(glaciers))]
    divide['RGIId'] = new_id

    for i in divide[divide.type != 'Polygon'].index:
        divide.loc[i, 'remarks'] = 'type != Polygon'

    divide = divide[~divide.geometry.is_empty]
    # check area
    divide['Area'] = ""

    cor_factor = float(outline.Area) / glaciers.Area.sum()

    if len(divide) < 2:
        rgidf.loc[index, 'remarks'] = [
            gdir.rgi_id + ' is too small or has no valid divide...']
    elif not keep:
        rgidf.loc[index, 'remarks'] = [' all divides were filtered']
    else:
        area_km = cor_factor * glaciers.Area

        # change cenlon,cenlat
        cenlon = [g.centroid.xy[0][0] for g in divide.geometry]
        cenlat = [g.centroid.xy[1][0] for g in divide.geometry]

        # write data
        divide.loc[divide.index, 'CenLon'] = cenlon
        divide.loc[divide.index, 'CenLat'] = cenlat
        # divide.loc[divide.index, 'geometry'] = new_geo
        divide.loc[divide.index, 'Area'] = area_km
        divide.loc[divide.index, 'OGGM_Area'] = glaciers.Area
        rgidf = rgidf.append(divide, ignore_index=True)

    if cor_factor > 1.2 or cor_factor < 0.8:
        rgidf.loc[index, 'remarks'] = [
            'sum of areas did not correlate with RGI_area']

    return rgidf


if __name__ == '__main__':

    cfg.initialize()
    cfg.PARAMS['continue_on_error'] = True
    cfg.PARAMS['use_multiprocessing'] = True
    # set dem resolution to 40 meters
    cfg.PARAMS['grid_dx_method'] = 'fixed'
    cfg.PARAMS['fixed_dx'] = 40
    cfg.PARAMS['use_intersects'] = False
    cfg.PARAMS['border'] = 10
    cfg.PATHS['working_dir'] = os.environ.get('S_WORKDIR')

    rgi_region = str(os.environ.get('REGION')).zfill(2)
    rgi_file = os.environ.get('RGI_DATA')
    dir_name = [d for d in os.listdir(rgi_file) if
                d.startswith(rgi_region) and d.endswith(".zip")]
    zip_ref = zipfile.ZipFile(os.path.join(rgi_file, dir_name[0]), 'r')
    zip_ref.extractall(cfg.PATHS['working_dir'])
    shp_name = [name for name in zip_ref.namelist() if name.endswith(".shp")][
        0]
    zip_ref.close()
    rgi_file = os.path.join(cfg.PATHS['working_dir'], shp_name)

    rgidf = salem.read_shapefile(rgi_file, cached=True)
    rgidf['remarks'] = ""
    gdirs = workflow.init_glacier_regions(rgidf, reset=False)

    # path to python 2.7
    python = '/home/users/julia/python2_env/bin/python'
    # path where partioning is located
    project = '/home/users/julia'
    script = os.path.join(project, 'partitioning/examples/run_divides.py')

    start1 = time.time()
    pool = mp.Pool()
    pool.map(run_div, gdirs)
    pool.close()
    pool.join()

    filter_option = ['no_filter', 'alt_filter', 'all_filter']

    filter_option = ['no_filter', 'alt_filter', 'all_filter']
    for filter in filter_option:
        new_rgi = copy.deepcopy(rgidf)
        for gdir in gdirs:
            new_rgi = postprocessing(new_rgi, gdir,
                                     gdir.get_filepath('outlines'), filter)

        sorted_rgi = new_rgi.sort_values('RGIId')
        sorted_rgi = sorted_rgi[['Area', 'OGGM_Area', 'Aspect', 'BgnDate',
                                 'CenLat', 'CenLon', 'Connect', 'EndDate',
                                 'Form', 'GLIMSId', 'Linkages', 'Lmax', 'Name',
                                 'O1Region', 'O2Region', 'RGIId', 'Slope',
                                 'Status', 'Surging', 'TermType', 'Zmax',
                                 'Zmed', 'Zmin', 'geometry', 'min_x', 'max_x',
                                 'min_y', 'max_y', 'remarks']]
        new_name = str(gdir.rgi_region) + '_dgi60_'+filter+'.shp'
        sorted_rgi.to_file(os.path.join(cfg.PATHS['working_dir'], new_name))