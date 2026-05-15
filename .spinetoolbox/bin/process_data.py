# -----------------------------------------------------------------------------------------
# Name:        process_data
# Purpose:     This routine is used to read the output files created by CWatM
#              and to be passed to FlexTool. The files that needs to be used and modified 
#              are defined here and FlexTool process should be called from here. 
#              FlexTool should return the modified files with the same name. These files are
#              then saved and located in the output folder that will be re-imported into the
#              next daily run of CWatMl
#
# Author:      Jean-Nicolas Louis
#
# Created:     15/07/2024
# Copyright:   (c) JNL 2024-2026
# -----------------------------------------------------------------------------------------

from os import listdir
from os.path import isfile, join
import netCDF4
import numpy as np
import sys
import xarray
import configparser
from pathlib import Path
from pathlib import PureWindowsPath  
import shutil
from CWatM_Module.management_modules.messages import *
from CWatM_Module.management_modules.globals import *
import difflib  # to check the closest word in settingsfile, if an error occurs
import extract_netcdf_data as end
from spinedb_api import DatabaseMapping
from datetime import datetime

#from chart_studio.plotly import plot, iplot
#from plotly.graph_objs import *
#from scipy.io import netcdf  
#from mpl_toolkits.basemap import Basemap

'''Debugging'''
#filepath = "C:/Users/JLJEAN/.spinetoolbox/work/export_to_ini_calib__9e5c7cb9f5574551a972f8870446f7f4__toolbox/output"
# export_to_ini_calib__1295f83d2f4546cbba34c611141cd901__toolbox
#f_in = filepath
#inifile = "C:/Users\\jljean\\.spinetoolbox\\work\\export_to_ini_calib__fa78697351f54b30b1a0010c1122fde8__toolbox/cwatm_input.ini"
#url = "sqlite:///c:\\git\\ca_model\\nexus_time_settings_only.sqlite"
'''in file'''
f_in = "./init"
inifile = sys.argv[1]
url = sys.argv[2]
class ExtParser(configparser.ConfigParser):
    """
    addition to the parser to replace placeholders

    Example:
        PathRoot = C:/work
        MaskMap = $(FILE_PATHS:PathRoot)/data/areamaps/area.tif

    """

    #implementing extended interpolation
    def __init__(self, *args, **kwargs):
        self.cur_depth = 0
        configparser.ConfigParser.__init__(self, *args, **kwargs)

    def get(self, section, option, raw=False, vars=None, **kwargs):
        """
        def get(self, section, option, raw=False, vars=None
        placeholder replacement

        :param section: section part of the settings file
        :param option: option part of the settings file
        :param raw:
        :param vars:
        :return:
        """

        #h1 = sys.tracebacklimit
        #sys.tracebacklimit = 0  # no long error message
        try:
           r_opt = configparser.ConfigParser.get(self, section, option, raw=True, vars=vars)
        except:
             print(section, option)
             closest = difflib.get_close_matches(option, list(binding.keys()))
             if not closest: closest = ["- no match -"]
             msg = "Error 116: Closest key to the required one is: \"" + closest[0] + "\""
             raise CWATMError(msg)

        #sys.tracebacklimit = h1   # set error message back to default
        if raw:
            return r_opt

        ret = r_opt
        self.cur_depth = self.cur_depth - 1
        return ret

def get_nc_files():

    ''' Get all the input files '''
    # All *.nc files need to be listed
    all_nc_files = [f for f in listdir(f_in) if isfile(join(f_in, f))]

    '''Save all the nc file in the output folder to be re-imported into the CWatM folder'''

    '''
    # save all files in the output folders NetCDF3
    for f in onlyfiles:
        file2read = netcdf.NetCDFFile("./init/" + f,'r')
        print(file2read.variables.keys())
        #temp = file2read.variables # var can be 'Theta', 'S', 'V', 'U' etc..
    '''
    # for NetCDF4
    for f in all_nc_files:
        file_content = netCDF4.Dataset(f_in + "/" + f)
        print("-------------" + f + "--------------") 
        # Get all the metadata/attributes to be written in the output file
        with netCDF4.Dataset(f,mode='w',format='NETCDF4_CLASSIC') as ncfile:
            for name in file_content.ncattrs():
                # Print("Global attr {} = {}".format(name, getattr(file_content, name)))
                setattr(ncfile, name, getattr(file_content, name))        
                
            # Create dimensions for the .nc file
            for f_dim in file_content.dimensions:
                dim_name = f_dim
                lat_dim = ncfile.createDimension(f_dim, file_content.dimensions[f_dim].size) # latitude axis
                # Get all the variables from each file
            for varname in file_content.variables.keys():
                temp = file_content.variables[varname]
                datatype = temp.datatype
                if datatype.name == "float32":
                    dt_in = np.float32
                elif datatype.name == "float64":
                    dt_in = np.float64
                else:
                    print("Not yet defined: " + datatype.name)
                add_extra = False
                if "_FillValue" in temp.ncattrs() or "_ChunkSizes" in temp.ncattrs():
                    add_extra = True
                var_in = ncfile.createVariable(varname, dt_in, temp.dimensions)
                for var_att in temp.ncattrs():
                    if var_att != "_FillValue" and var_att != "_ChunkSizes":
                        setattr(var_in, var_att, getattr(temp, var_att))

                # Add values to the variable
                if len(temp.dimensions) == 1:
                    var_in[:] = temp[:]
                elif len(temp.dimensions) == 2:
                    var_in[:,:] = temp[:,:]
                elif len(temp.dimensions) == 3:
                    var_in[:,:,:] = temp[:,:,:]
                elif len(temp.dimensions) == 4:
                    var_in[:,:,:,:] = temp[:,:,:,:]
                else:
                    print("missing data dimensions to be added. add more dimensions in the script")
                    
                # Read the data from within the 

def parse_ini(ini):
    # Read the ini file
    config = ExtParser()
    config.optionxform = str
    config.sections()
    config.read(ini)
    return config

def combine_outputs(ini):
    # Read the ini file
    config = parse_ini(ini)
    # Select a fixed output path to store the final outputs
    outpath = os.path.join(config['FILE_PATHS']["PathCombinednc"], '')
    # Get the current output to merge with from PathOut
    currentoutput = os.path.join(config['FILE_PATHS']["PathOut"], '')
    # Get a list of each output
    all_nc_files = list(Path(currentoutput).rglob("*.nc"))
    # Get the loop count variable to see if this is the first loop or not
    loopcount = config['OPTIONS']["loopcount"]    
    if loopcount=="false":
            # Copy all the nc files to the final output locations
            print(f"Moving output file to: {outpath}")
            for f in all_nc_files:
                #print(f)
                shutil.move(PureWindowsPath(f), outpath)
            return
    # Get the initload path
    #initpath  = config['INITITIAL CONDITIONS']["initLoad"]
    #spath = initpath.replace('\\',' ').replace('/',' ').split()
    #sprevious = spath[:-2]
    #previousoutput = '/'.join(sprevious) + "/output"  
    
    for file in all_nc_files:
        daily = False
        time = True
        var = file.name[:-3]
        if file.name[:-3].split('_')[-1] == 'daily':
            var = var[:-6]
            daily = True

        file_names = file.name
        listfiles = [outpath + "/" + file_names,currentoutput + "/" + file_names]
        if daily:  
            with xarray.open_mfdataset(listfiles,combine = 'nested', concat_dim="time") as combined:
                file_path = Path(f"{outpath}{file_names}")
                # Write the file to a different name to prevent xarray errors
                if file_path.exists():
                    combined.to_netcdf(f"{outpath}bis{file_names}", mode='a')
                else:
                    combined.to_netcdf(f"{outpath}bis{file_names}")
                
                        # Rename the file to its original name after it has been saved
            old_file = f"{outpath}bis{file_names}"
            new_file = f"{outpath}{file_names}"
            original_file = f"{currentoutput}/{file_names}"
            #print("Cleaning the place...")
            #print(f"    Removing old files: {outpath}{file_names}")
            os.remove(new_file)
            #print(f"    Removing original files: {currentoutput}/{file_names}")
            os.remove(original_file)
            #print(f"    Renaming output file")
            os.rename(old_file, new_file)
        else:
            # This means the file does not have a time dimension and can simply be replaced by the current output
            if os.path.isfile(outpath + "/" + file_names):
                os.remove(outpath+'/'+ file_names)
                #print(file_names, 'has been removed from: ', outpath)   
            shutil.move(os.path.join(currentoutput, file_names), os.path.join(outpath, file_names))
            #print("New file has been moved to:", outpath)

def extract_cdf_data(url, ini):
    # python extract_nc_timeseries.py data.nc --lat 60.17 --lon 24.94 --start 2005-06-01 --end 2010-12-31 --csv output.csv
    # Get the nc file targeted from the warm start
    config = parse_ini(ini)
    datedt = datetime.strptime(config['INITITIAL CONDITIONS']["StepInit"], '%d/%m/%Y')
    initfile = config['INITITIAL CONDITIONS']["initSave"] + "_" + datedt.strftime('%Y%m%d') + ".nc"
    currentoutput = os.path.join(config['FILE_PATHS']["PathOut"], '')
    all_nc_files = list(Path(currentoutput).rglob("*.nc"))
    selected_nc_file = config['OUTPUT']["OUT_TSS_Daily"]
    print(selected_nc_file)
    # get the start and end dates
    stepstartimport = datetime.strptime(config['TIME-RELATED_CONSTANTS']["StepStart"], '%d/%m/%Y')
    stepstart = stepstartimport.strftime('%Y-%m-%d')
    stependimport = datetime.strptime(config['TIME-RELATED_CONSTANTS']["StepEnd"], '%d/%m/%Y')
    stepend = stependimport.strftime('%Y-%m-%d')
    # List the dam names to be extracted. the names must match the unit names from the FlexTool database.
    dam_names = ["rogun"]  # example dam names, replace with actual names
    # Get the lat and lon from the settings file
    with DatabaseMapping(url) as db_map:
        db_map.fetch_all("entity")  # Prefetch data. May provide a speed boost for later operations.
        for unit in db_map.find_entities(entity_class_name="unit"):
            if unit["name"].endswith("_spill") and any(xs in unit["name"] for xs in dam_names):
                # deal with spills.
                print((unit["name"], unit["lat"], unit["lon"]))
                # Get the start and end date from the settings file
                # Call the function to extract the data and save it in a csv file
                ## Optional input 
                """
                        Extract a time series for a given lat/lon point.

                        Parameters
                        ----------
                        nc_file     : str   – path to the NetCDF file
                        lat         : float – target latitude
                        lon         : float – target longitude
                        year        : int   – single year to extract (mutually exclusive with date range)
                        date_start  : str   – start date string 'YYYY-MM-DD' (use with date_end)
                        date_end    : str   – end date string   'YYYY-MM-DD' (use with date_start)
                        variable    : str   – variable name (auto-detected if None)
                        plot        : bool  – show a quick matplotlib plot
                        csv_file    : str   – path to save results as CSV (optional)
                        """
                for file in all_nc_files:
                    daily = False
                    time = True
                    var = file.name[:-3]
                    if file.name[:-3].split('_')[-1] == 'daily':
                        var = var[:-6]
                        daily = True
                    
                    file_names = file.name
                    if var in selected_nc_file.split(','):
                        listfiles = [currentoutput + file_names]
                        print(f"Extracting data for {unit['name']} from file: {file_names}")
                        end.extract_timeseries(
                            nc_file=listfiles[0],
                            lat=unit["lon"],
                            lon=unit["lat"],
                            date_start=stepstart,
                            date_end=stepend,
                            variable="discharge",
                            csv_file="output.csv")
    # set the name of the output file (atm hardcoded, but can be defined in the settings file)

def main():
    debugging = False
    if debugging:
        print("nothing to pass")
        print("continue with the next day in CWatM")
    else:
        extract_cdf_data(url, inifile)
        combine_outputs(inifile)
        
        # For coupling purposes, we can alter the init file that are generated by CWatM. These init file has multiple variables that can be read from the river basin
        # The output files are not re-used by CWatM, so they are as they are.
        #get_nc_files()


if __name__ == "__main__":
    main()