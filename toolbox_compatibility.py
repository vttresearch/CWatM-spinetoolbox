import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "CWatM"))

import subprocess
from cwatm.management_modules.globals import *
from cwatm.management_modules.messages import *
import difflib
import configparser
from pathlib import Path
import io
import time
import shutil 

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
def replacetext(file, search_text, replace_text): 
  
    # Opening the file using the Path function 
    file = Path(r"{}".format(file))
  
    # Reading and storing the content of the file in 
    # a data variable 
    data = file.read_text() 
  
    # Replacing the text using the replace function 
    data = data.replace(search_text, replace_text) 
  
    # Writing the replaced data 
    # in the text file 
    file.write_text(data) 
  
    # Return "Text replaced" string 
    return 

def main():
    path = sys.argv[0]
    inifile = sys.argv[1]

    if not(os.path.isfile(inifile)):
            msg = "Error 302: Settingsfile not found!\n"
            raise CWATMFileError(inifile,msg)
    config = ExtParser()
    config.optionxform = str
    config.sections()
    config.read(inifile)

    # Get the output path from the config file
    outputfolder = config['FILE_PATHS']["PathOut"]
    print('PathOut = ' + outputfolder)

    # Save the ini file to be re-used
    #shutil.copyfile(inifile,inifile)     
    # Replace the values in the initial conditions from the database and reset the pathfiles.
    initfolder = config["INITITIAL CONDITIONS"]["initSave"]
    current_directory = os.getcwd()
    final_directory = os.path.join(current_directory, Path(r"{}".format(outputfolder)))
    final_directory_init = os.path.join(current_directory, Path(r"{}".format(initfolder)))
    if not os.path.exists(final_directory):
        print("Creating the directory: " + final_directory)
        os.makedirs(final_directory)
    if not os.path.exists(final_directory_init):
        print("Creating the directory: " + final_directory_init)
        os.makedirs(final_directory_init)
                # Re-write the path to the dictionnary to be used in the ini file
    #PathOut = .\output_30min4

    # Create the output folder as given in the database from the ini file
    replacetext(inifile, 'PathOut = ' + outputfolder, 'PathOut = ' + final_directory)
    replacetext(inifile, 'initSave = ' + initfolder, 'initSave = ' + final_directory_init)

    print(sys.argv[1:])

    # Call cwatm 
    filename = "toolbox_cwatm.log"
    process = subprocess.Popen(["python", "run_cwatm.py"] + sys.argv[1:])
    output, errors = process.communicate()
    f = open(filename,'w')
    content = "OUTPUT:\n"+str(output)+"\nERRORS:\n"+str(errors)
    f.write(content)
    f.close()

#subprocess.run(['python', 'run_cwatm.py'] + sys.argv[1:])
if __name__ == "__main__":
    main()