import os
import time
import rasterio
import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib.pyplot as plt
from osgeo import gdal, ogr
from rasterio import features
from scipy.ndimage import gaussian_filter1d



def load_raster(in_path):
    raster = dict()

    dataset = gdal.Open(in_path)
    band = dataset.GetRasterBand(1)
    band.ComputeStatistics(0)
    raster['data'] = band.ReadAsArray()

    raster['nd_value'] = band.GetNoDataValue()
    raster['cols'] = dataset.RasterXSize
    raster['rows'] = dataset.RasterYSize
    raster['crs'] = dataset.GetProjectionRef()
    raster['transform'] = dataset.GetGeoTransform()
    raster['origin_x'] = raster['transform'][0]
    raster['origin_y'] = raster['transform'][3]
    raster['pixel_width'] = raster['transform'][1]
    raster['pixel_height'] = raster['transform'][5]
    
    x_max = raster['origin_x'] + (raster['cols'] * raster['pixel_width'])
    y_min = raster['origin_y'] + (raster['rows'] * raster['pixel_height'])
    raster['bbox'] = (raster['origin_x'], y_min, x_max, raster['origin_y'])

    return raster

def gage_areas_from_poly(shp_path, id_field, dem_filter, save_path=None):
    # Get some data from the raster template
    dtype = 'float64'
    bbox = dem_filter['bbox']
    affine = rasterio.Affine.from_gdal(*dem_filter['transform'])
    in_mask = (dem_filter['data'] == dem_filter['nd_value']).reshape(dem_filter['data'].shape)
    
    # Load gage polygons and reformat
    subbasins = gpd.read_file(shp_path, bbox=bbox)
    subbasins[id_field] = subbasins[id_field].astype(dtype)
    subbasins = list(subbasins[['geometry', id_field]].itertuples(index=False, name=None))

    # Rasterize polygons and return
    if save_path:
        with rasterio.open(save_path, 'w+', driver='GTiff', width=dem_filter['cols'], height=dem_filter['rows'], count=1, 
                        crs=dem_filter['crs'], transform=affine, dtype=dtype, nodata=dem_filter['nd_value']) as out:
            out_array = out.read(1)
            rasterized = features.rasterize(shapes=subbasins, fill=dem_filter['nd_value'], out=out_array, transform=out.transform)
            rasterized[in_mask] = dem_filter['nd_value']  # Mask by subbasin outline
            out_array[in_mask] = dem_filter['nd_value']  # Mask by subbasin outline
            out.write_band(1, rasterized)
    else:
        rasterized = features.rasterize(shapes=subbasins, fill=dem_filter['nd_value'], out=np.full(dem_filter['data'].shape, dem_filter['nd_value'], dtype=dtype), transform=affine)
        rasterized[in_mask] = dem_filter['nd_value']  # Mask by subbasin outline

    out_rast = dem_filter.copy()
    out_rast['data'] = rasterized
    return out_rast

def gage_areas_from_poly_gdal(shp_path, id_field, dem_filter, save_path=None, reaches=None):
    # Load gage polygons and reformat
    subunits = ogr.Open(shp_path)
    subunits_layer = subunits.GetLayer()
    if reaches is not None:
        subunits_layer.SetAttributeFilter(f"{id_field} in {tuple(reaches)}")

    # Rasterize polygons and return
    nd_value = -9999
    if save_path:
        target_ds = gdal.GetDriverByName('GTiff').Create(save_path, dem_filter['cols'], dem_filter['rows'], 1, gdal.GDT_Int64)
    else:
        target_ds = gdal.GetDriverByName('MEM').Create('', dem_filter['cols'], dem_filter['rows'], 1, gdal.GDT_Int64)

    target_ds.SetGeoTransform((dem_filter['origin_x'], dem_filter['pixel_width'], 0, dem_filter['origin_y'], 0, dem_filter['pixel_height']))
    band = target_ds.GetRasterBand(1)
    target_ds.SetProjection(dem_filter['crs'])
    band.SetNoDataValue(nd_value)

    options = [f"ATTRIBUTE={id_field}", "outputType=gdal.GDT_Int64"]

    gdal.RasterizeLayer(target_ds, [1], subunits_layer, options=options)

    thiessen = dem_filter.copy()
    thiessen['data'] = band.ReadAsArray()

    return thiessen


def reach_hydraulics(r, thiessens, elevations, slope, el_nd, resolution, bins):
    mask = thiessens == r  # Select cells within reach area of interest
    mask = np.logical_and(mask, elevations != el_nd)  # Select cells with valid HAND elevation
    mask = np.logical_and(mask, elevations < bins.max())  # Select cells with HAND elevation within range of interest
    tmp_elevations = elevations[mask]
    tmp_slope = np.arctan(slope[mask])
    projected_area = (resolution ** 2) / np.cos(tmp_slope)  # Wetted perimeter
    
    wrk_df = pd.DataFrame({'el': tmp_elevations, 'p': projected_area})
    wrk_df['bins'] = pd.cut(wrk_df['el'], bins=bins, labels=bins[:-1], include_lowest=True)  # Binning a la semivariogram
    wrk_df = wrk_df.groupby(wrk_df['bins']).agg(el=('el', 'mean'),
                                                count=('el', 'count'),
                                                p=('p', 'sum'))
    wrk_df['el'] = wrk_df['el'].fillna(wrk_df.index.to_series().astype('float'))
    
    wrk_df['area'] = wrk_df['count'].cumsum()
    wrk_df['area'] -= (wrk_df['count'] * 0.5)  # Center
    tmp_p = wrk_df['p'].cumsum()
    tmp_p -= (wrk_df['p'] * 0.5)  # Center
    wrk_df['p'] = tmp_p

    depth_change = bins[1] - bins[0]
    vol_increase = depth_change * wrk_df['area']
    wrk_df['vol'] = np.cumsum(vol_increase)
    wrk_df['rh'] = wrk_df['vol'] / wrk_df['p']
    wrk_df['rh_prime'] = (wrk_df['rh'].shift(-1) - wrk_df['rh']) / depth_change
    
    p_prime = (wrk_df['p'].shift(-1) - wrk_df['p']) / depth_change
    k_prime = (5 / 3) - ((2 / 3) * (1 / wrk_df['area']) * wrk_df['rh'] * p_prime)
    wrk_df['celerity'] = k_prime * (wrk_df['rh'] ** (2 / 3))

    return wrk_df


def subunit_hydraulics(hand_path, aoi_path, slope_path, stages, reach_field=None, reaches=None, fields_of_interest=None):
    elevations = load_raster(hand_path)
    slope = load_raster(slope_path)
    if aoi_path[-3:] == 'tif':
        thiessens = load_raster(aoi_path)
    elif aoi_path[-3:] == 'shp':
        thiessens = gage_areas_from_poly_gdal(aoi_path, reach_field, elevations, reaches=reaches, save_path=r'C:\Users\klawson1\Documents\CIROH_Floodplains\winooski\subbasins\0504\rasters\gage_areas.tif')

    resolution = elevations['pixel_width'] * elevations['pixel_height']

    data_dict = {k: pd.DataFrame() for k in fields_of_interest}

    counter = 0
    t1 = time.perf_counter()
    for r, s in zip(reaches, stages):
        print(f'{counter} / {len(reaches)}', end="\r")
        wrk_df = reach_hydraulics(r, thiessens['data'], elevations['data'], slope['data'], elevations['nd_value'], resolution, s)

        for k in data_dict:
            # data_dict[k] = pd.concat((data_dict[k], wrk_df[k].rename(r)), axis=1, ignore_index=True)
            data_dict[k][r] = wrk_df[k].reset_index(drop=True)
            # data_dict[k] = data_dict[k].reset_index(drop=True)

        counter += 1
    print('')
    print(f'Completed processing in {round(time.perf_counter() - t1, 1)} seconds')
    return data_dict


def extract_topographic_signature(hand_path, aoi_path, slope_path, reaches=None, max_el=10, nstages=1000, show=False, save_path=None, reach_field=None):
    elevations = load_raster(hand_path)
    slope = load_raster(slope_path)
    if aoi_path[-3:] == 'tif':
        thiessens = load_raster(aoi_path)
    elif aoi_path[-3:] == 'shp':
        thiessens = gage_areas_from_poly(aoi_path, reach_field, elevations)

    resolution = elevations['pixel_width'] * elevations['pixel_height']
    stages = np.linspace(0, max_el, nstages, endpoint=True)

    all_reaches = np.unique(thiessens['data'])
    if reaches == None:
        reaches = [r for r in all_reaches if r != thiessens['nd_value']]  # filter out nodata
    else:
        reaches = list(set(all_reaches).intersection([int(i) for i in reaches]))

    for r in reaches:
        print(r)
        wrk_df = reach_hydraulics(r, thiessens['data'], elevations['data'], slope['data'], elevations['nd_value'], resolution, stages)

        fig, ax = plt.subplots()
        ax.plot(wrk_df['el'], wrk_df['rh_prime'], label='raw')
        ax.plot(wrk_df['el'], gaussian_filter1d(wrk_df['rh_prime'], 3), label='smoothed')
        ax.set_xlabel('Stage (m)')
        ax.set_ylabel(r"$R_{h}$ '")
        ax.set_ylim(-3, 1)
        ax.set_title(r)
        plt.legend()
        if show:
            plt.show()
        if save_path:
            tmp_path = os.path.join(save_path, f'{r}.png')
            fig.savefig(tmp_path, dpi=300)
        
        plt.close(fig)

def plot_rh_curve(hand_path, aoi_path, slope_path, reaches=None, max_el=10, nstages=1000, show=False, save_path=None, reach_field=None):
    elevations = load_raster(hand_path)
    slope = load_raster(slope_path)
    if aoi_path[-3:] == 'tif':
        thiessens = load_raster(aoi_path)
    elif aoi_path[-3:] == 'shp':
        thiessens = gage_areas_from_poly(aoi_path, reach_field, elevations)

    resolution = elevations['pixel_width'] * elevations['pixel_height']

    all_reaches = np.unique(thiessens['data'])
    if reaches == None:
        reaches = [r for r in all_reaches if r != thiessens['nd_value']]  # filter out nodata
    else:
        reaches = list(set(all_reaches).intersection([int(i) for i in reaches]))

    for r in reaches:
        print(r)
        wrk_df = reach_hydraulics(r, thiessens['data'], elevations['data'], slope['data'], elevations['nd_value'], nstages, resolution, max_el)

        fig, ax = plt.subplots()
        ax.plot(wrk_df['el'], wrk_df['rh'])
        ax.set_xlabel('Stage (m)')
        ax.set_ylabel(r"$R_{h}$")
        ax.set_ylim(0, 10)
        ax.set_title(r)
        if show:
            plt.show()
        if save_path:
            tmp_path = os.path.join(save_path, f'{r}.png')
            fig.savefig(tmp_path, dpi=300)
        
        plt.close(fig)


def extract_celerity_signature(hand_path, aoi_path, slope_path, reaches=None, max_el=10, nstages=1000, show=False, save_path=None):
    elevations, el_meta = load_raster(hand_path)
    thiessens, thiessen_meta = load_raster(aoi_path)
    slope, slope_meta = load_raster(slope_path)

    resolution = el_meta['pixel_width'] * el_meta['pixel_height']

    all_reaches = np.unique(thiessens)
    if reaches == None:
        reaches = [r for r in all_reaches if r != thiessen_meta['nd_value']]  # filter out nodata
    else:
        reaches = list(set(all_reaches).intersection([int(i) for i in reaches]))

    for r in reaches:
        print(r)
        mask = thiessens == r
        mask = np.logical_and(mask, elevations != el_meta['nd_value'])
        mask = np.logical_and(mask, elevations < max_el)
        tmp_elevations = elevations[mask]
        tmp_slope = np.arctan(slope[mask])
        projected_area = (resolution ** 2) / np.cos(tmp_slope)

        bins = np.linspace(0, 10, nstages, endpoint=True)
        wrk_df = pd.DataFrame({'el': tmp_elevations, 'p': projected_area})
        wrk_df['bins'] = pd.cut(wrk_df['el'], bins=bins, labels=bins[:-1], include_lowest=True)
        wrk_df = wrk_df.groupby(wrk_df['bins']).agg(el=('el', 'mean'),
                                                    count=('el', 'count'),
                                                    p=('p', 'sum'))
        
        wrk_df['area'] = wrk_df['count'].cumsum()
        wrk_df['area'] -= (wrk_df['count'] * 0.5)
        wrk_df['p'] = wrk_df['p'].cumsum()

        depth_change = bins[1] - bins[0]
        vol_increase = depth_change * wrk_df['area']
        wrk_df['vol'] = np.cumsum(vol_increase)
        wrk_df['rh'] = wrk_df['vol'] / wrk_df['p']
        wrk_df['rh_prime'] = (wrk_df['rh'].shift(-1) - wrk_df['rh']) / depth_change

        wrk_df['p_prime'] = (wrk_df['p'].shift(-1) - wrk_df['p']) / depth_change
        k_prime = (5 / 3) - ((2 / 3) * (1 / wrk_df['area']) * wrk_df['rh'] * wrk_df['p_prime'])
        wrk_df['celerity'] = k_prime * (wrk_df['rh'] ** (2 / 3))

        fig, ax = plt.subplots()
        ax.plot(wrk_df['el'], wrk_df['celerity'], label='raw')
        ax.plot(wrk_df['el'], gaussian_filter1d(wrk_df['celerity'], 3), label='smoothed')
        ax.set_xlabel('Stage (m)')
        ax.set_ylabel('Kinematic Celertiy')
        ax.set_title(r)
        plt.legend()
        if show:
            plt.show()
        if save_path:
            tmp_path = os.path.join(save_path, f'{r}.png')
            fig.savefig(tmp_path, dpi=300)
        plt.close(fig)
