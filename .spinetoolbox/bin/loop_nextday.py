# -----------------------------------------------------------------------------------------
# Name:        loop_nextday
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
# Copyright:   (c) JNL 2024
# -----------------------------------------------------------------------------------------

import configparser
from CWatM_Module.management_modules.globals import *
from CWatM_Module.management_modules.messages import *
import difflib  # to check the closest word in settingsfile, if an error occurs
import datetime
from datetime import timedelta
import sys
from spinedb_api import DatabaseMapping
import json
import spinedb_api as api
import collections.abc
import glob 
import shutil
import xarray
from pathlib import Path, PureWindowsPath
import os

# get the ini file
inifile = sys.argv[1]
#inifile = "C:/Users/JLJEAN/.spinetoolbox/work/process_to_flextool__92b8877f66b04dcbba370bca4ebb5c39__toolbox/cwatm_input.ini"
# Get the database URL
url = sys.argv[3]
#url="sqlite:///c:\git\cwatm-spinetoolbox-dev\.spinetoolbox\items\data_store\cwatmdb_new.sqlite"
# Get the alternatives for the scenario looping where variables can be changed into
#alt_file = "C:/Users/JLJEAN/.spinetoolbox/work/export_to_ini_calib__3577b232e0bb442d9a5c21c5364fa51b__toolbox/alt_list.json"
alt_file = sys.argv[2]
print(alt_file)
with open(alt_file) as f:
    data_alt = json.load(f)

print(data_alt)
# Declare the alternative where the calibration variables have been declared
with DatabaseMapping(url) as db_map:
    real_url = db_map.db_url 
with open("out.dat", "w") as out_file:
    out_file.writelines([f"{real_url}\n"])

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

def writealt_to_file(var, alt):
    with open("out.dat", "a") as out_file:
        out_file.writelines([f"{var};{alt} \n"])

def allocate_var_to_alt(var, newvalue, highrank, data_alt, sql_url, ECN):
    #value, value_type = api.to_database(newvalue)
    print(f"{newvalue} of type {type(newvalue)}")
    if isinstance(newvalue, bool) or isinstance(newvalue, str):
        value, value_type = api.to_database(newvalue)
        print(f"The variable {var} with value {newvalue} is of type bool or str")
        # Check if it is a string
    elif isinstance(newvalue, datetime.date):
        parsed_value = api.DateTime(newvalue.strftime("%Y-%m-%d"))
        value, value_type = api.to_database(parsed_value)
    elif isinstance(newvalue, collections.abc.Sequence):
        #print(data[key2])
        if isinstance(newvalue, datetime.date):
            parsed_value = api.Array([dates.strftime("%Y-%m-%d") for dates in newvalue])
        else:
            parsed_value = api.Array(newvalue)
            value, value_type = api.to_database(parsed_value)
    elif isinstance(newvalue, float) or isinstance(newvalue, int):
        print(f"The variable {var} with value {newvalue} is of type float or int")
        value, value_type = api.to_database(newvalue)
    # Loop through the alternative and exit when the it is found (from highest to lowest ranked)
    foundalt = False
    highrankmax = highrank
    while highrank > 0:
        alt_name = data_alt[str(highrank)]
        with DatabaseMapping(sql_url) as db_map:
            param_val = db_map.get_parameter_value_item(entity_class_name=ECN, entity_byname=(ECN,), parameter_definition_name=var, alternative_name=alt_name)
            if not param_val:
                print(f"The variable {var} does not exists in the alternative {alt_name}")
                highrank -= 1
                if highrank == 0:
                    # This means this is the last loop and the variable does not exist in any alternative. Create the variable in the last alternative and 
                    # Commit the database
                    alt_name = data_alt[str(highrankmax)]
                    print(f"The variable {var} does not exists in any alternative. Allocating it to alternative {alt_name}")
                    
                    # Add the parameter definition as it does not seem to exist
                    try:
                        db_map.add_parameter_definition(
                            entity_class_name=ECN,
                            name = var
                        )
                    except:
                        print(f"there's already a parameter_definition with 'entity_class_name': {ECN}, 'name': {var}")   
                    # Add a value to this parameter. This will not work if the coupling alternative is not present in the list of alternatives
                    db_map.add_parameter_value(
                        entity_class_name=ECN,
                        entity_byname=(ECN,),
                        parameter_definition_name=var,
                        alternative_name=alt_name,
                        value=value,
                        type=value_type
                        )
                    try:
                        db_map.commit_session(f"Updated variable {var} in a loop in alternative {alt_name}")
                        print("Database committed")
                    except Exception as error:
                        print("nothing to commit:", error)
            else:
                foundalt = True
                writealt_to_file(var, alt_name)
                print(f"    The variable {var} exists in the alternative {alt_name}")
                data_spdb = api.from_database(param_val.get("value"), param_val.get("type"))
                try:
                    print(f"    Old value: {str(data_spdb.value)} - New value: {str(value)}")
                except:
                    print("     new value was found")
                highrank = 0

                db_map.get_parameter_value_item(
                    entity_class_name=ECN,
                    entity_byname=(ECN,),
                    parameter_definition_name=var,
                    alternative_name=alt_name,
                    ).update(value=value, type=value_type)
                
                try:
                    db_map.commit_session(f"Updated variable {var} in a loop in alternative {alt_name}")
                    print("Database committed")
                except Exception as error:
                    print("nothing to commit:", error)
        
    
    if not foundalt:
        print(f"The variable {var} was not found in the database")

    return

def convert_time(date):
    if isinstance(date, datetime.datetime):
        date = date.date()
    if isinstance(date, str):
        date = datetime.datetime.strptime(date, '%d/%m/%Y')
        date = date.date()
    return date

def getncfilename(initsave, stepend):
    """Build the CWatM warm-start file path saved at ``stepend``.

    CWatM writes the init file as ``{initSave}_{YYYYMMDD}.nc`` at the end of
    its simulation, so the file to load next is the ``initSave`` prefix with
    the just-finished run's ``StepEnd`` date appended.
    """
    return f"{initsave}_{stepend:%Y%m%d}.nc"


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
    #create the output folder if it does not exist
    if not os.path.exists(outpath):
        print("Creating the directory: " + outpath)
        os.makedirs(outpath)
    # Get the current output to merge with from PathOut
    currentoutput = os.path.join(config['FILE_PATHS']["PathOut"], '')
    # Get a list of each output
    all_nc_files = list(Path(currentoutput).rglob("*.nc"))
    print(currentoutput)
    print(all_nc_files[0])
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
        print(list(Path(currentoutput).rglob("*.nc"))[0])
        file_names = file.name
        print(f"Processing file: {file_names}")
        listfiles = [outpath + "/" + file_names,currentoutput + "/" + file_names]
        if daily:
            if file_names not in os.listdir(outpath):
                shutil.copy(os.path.join(currentoutput, file_names), os.path.join(outpath, file_names))
                print(f"File {file_names} has been moved to: {outpath}")
            else:
                with xarray.open_mfdataset(listfiles,combine = 'nested', concat_dim="time") as combined:
                    file_path = Path(f"{outpath}{file_names}")
                    # Write the file to a different name to prevent xarray errors
                    if file_path.exists():
                        combined.to_netcdf(f"{outpath}bis{file_names}", mode='a')
                    else:
                        combined.to_netcdf(f"{outpath}bis{file_names}")
                    print(f"File {file_names} has been combined and saved to: {outpath}bis{file_names}")
                # Rename the file to its original name after it has been saved
                old_file = f"{outpath}bis{file_names}"
                new_file = f"{outpath}{file_names}"
                print("Cleaning the place...")
                print(f"    Removing old files: {outpath}{file_names}")
                print(list(Path(currentoutput).rglob("*.nc"))[0])
                if file_path.exists():
                    os.remove(new_file)
                print(f"    Renaming output file")
                os.rename(old_file, new_file)
            original_file = f"{currentoutput}/{file_names}"
            print(f"    Removing original files: {currentoutput}/{file_names}")
            os.remove(original_file)
        else:
            # This means the file does not have a time dimension and can simply be replaced by the current output
            if os.path.isfile(outpath + "/" + file_names):
                os.remove(outpath+'/'+ file_names)
                #print(file_names, 'has been removed from: ', outpath)   
            shutil.move(os.path.join(currentoutput, file_names), os.path.join(outpath, file_names))
            #print("New file has been moved to:", outpath)


def main():
    if not(os.path.isfile(inifile)):
        msg = "Error 302: Settingsfile not found!\n"
        raise CWATMFileError(inifile,msg)

    config = ExtParser()
    config.optionxform = str
    config.sections()
    config.read(inifile)

    # Combine the CWatM dispatch re-run outputs into the combined folder
    # (moved here from process_data.py).
    combine_outputs(inifile)

    # Get the Looping time 
    RollFlexTool = config['TIME-RELATED_CONSTANTS']["RollFlexTool"]
    RollFlexToolnum = int(RollFlexTool.replace('D', ''))
    # Get the Stepend value
    stepend = config['TIME-RELATED_CONSTANTS']["StepEnd"]
    stepend = convert_time(stepend)
    # The just-finished run's end date names the CWatM warm-start file.
    laststepend = stepend

    # Set the start date when it previously stopped to enhance a warm start and go straight to the spinup time
    stepstart = stepend + timedelta(days=1)

    spinup = stepstart
    spinup = convert_time(spinup)

    # Define the new end date based on the rolling horizon
    stepend = stepstart + timedelta(days=RollFlexToolnum)- timedelta(days=1)

    StepFlexTool = config['TIME-RELATED_CONSTANTS']["StepFlexTool"]
    StepFlexTool = convert_time(StepFlexTool)

    if type(stepstart) != type(StepFlexTool):
        stepstart = convert_time(stepstart)
        StepFlexTool = convert_time(StepFlexTool)

    if type(stepend) != type(StepFlexTool):
        stepend = convert_time(stepend)
        StepFlexTool = convert_time(StepFlexTool)

    #if stepstart > StepFlexTool:
    #    stepend = min(stepend,StepFlexTool)
    #if stepend > StepFlexTool:
    stepend = min(stepend,StepFlexTool)

    # Either set it to 1D (for debugging purposes) or to stepend to get the last day of the simulation
    #unsure why this exists
    #stepinit = "01/01/1932 1d"
    #stepinit = "01/01/1932"
    if "loopcount" in config['OPTIONS']:
        loopcount = True
        print(f"The variable loopcount was found in the database, its value is {loopcount} of type {type(loopcount)}")
    else:
        loopcount = False
        print(f"The variable loopcount was not found in the database, its value is set to {loopcount}  of type {type(loopcount)}")


    load_initial = True

    # Need to find the initfile where it is saved and find its name
    initfolderload = config['INITITIAL CONDITIONS']["initSave"]

    # Build the init file path from initSave + the run's StepEnd date
    ncfilepath = getncfilename(initfolderload, laststepend)

    # Set the start date when it previously stopped to enhance a warm start and go straight to the spinup time
    

    # Combine the output files with the same name by concatenating the files.

    # Look for the variables in the database from the winning alternative to the lowest ranked alternative and change the value
    highrank = len(data_alt)
    #allocate_var_to_alt("StepInit", stepinit, highrank, data_alt, url, "INITITIAL CONDITIONS")
    allocate_var_to_alt("StepStart", stepstart, highrank, data_alt, url, "TIME-RELATED_CONSTANTS")
    allocate_var_to_alt("SpinUp", spinup, highrank, data_alt, url, "TIME-RELATED_CONSTANTS")
    allocate_var_to_alt("StepEnd", stepend, highrank, data_alt, url, "TIME-RELATED_CONSTANTS")
    allocate_var_to_alt("load_initial", load_initial, highrank, data_alt, url, "INITITIAL CONDITIONS")
    allocate_var_to_alt("loopcount", loopcount, highrank, data_alt, url, "OPTIONS")
    # Re-allocate the path of the init load based on the init save path
    allocate_var_to_alt("initLoad", ncfilepath, highrank, data_alt, url, "INITITIAL CONDITIONS")

    #print(stepinit)

if __name__ == "__main__":
    main()