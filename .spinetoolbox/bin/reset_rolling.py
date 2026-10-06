"""
Reset rolling time parameters in CWatM and FlexTool databases.

Reads StepStart, StepInit, SpinUp, StepEnd, and RollFlexTool from a text
file. Writes the time parameters to the CWatM Spine database and converts
RollFlexTool to hours for the FlexTool database's rolling_solve_jump parameter.

Usage:
    python reset_rolling.py <time_params.txt> <cwatm_db> <flextool_db>

Arguments:
    time_params.txt  - Text file with key=value pairs
    cwatm_db         - Path to CWatM Spine database (.sqlite)
    flextool_db      - Path to FlexTool Spine database (.sqlite)

The text file format is simple key=value pairs (one per line):
    StepStart = 01/01/2050
    StepEnd = 08/01/2050
    SpinUp = 01/01/2050
    StepInit = 08/01/2051
    RollFlexTool = 7D
"""
import re
import sys
import os
from pathlib import Path
from spinedb_api.exception import NothingToCommit

# Map each parameter to the CWatM DB entity class it belongs to
PARAM_SECTIONS = {
    "StepStart": "TIME-RELATED_CONSTANTS",
    "StepEnd": "TIME-RELATED_CONSTANTS",
    "SpinUp": "TIME-RELATED_CONSTANTS",
    "RollFlexTool": "TIME-RELATED_CONSTANTS",
    "StepInit": "INITITIAL CONDITIONS",
    "StepFlexTool": "TIME-RELATED_CONSTANTS",
    "PathOut": "FILE_PATHS",
    "loopcount": "OPTIONS",
    "initLoad": "INITITIAL CONDITIONS",
}

def read_params(txt_path):
    """Read key=value parameters from the text file."""
    params = {}
    with open(txt_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if key in PARAM_SECTIONS:
                    params[key] = val
    return params


def roll_to_hours(roll_str):
    """Convert RollFlexTool string (e.g. '7D', '168H') to hours."""
    m = re.match(r"(\d+)\s*([DHdh])", roll_str.strip())
    if not m:
        raise ValueError(f"Cannot parse RollFlexTool value: {roll_str!r}")
    val = int(m.group(1))
    unit = m.group(2).upper()
    if unit == "D":
        return val * 24
    return val


def update_cwatm_db(db_url, params):
    """
    Write time parameters to the CWatM Spine database.

    The CWatM DB uses entity classes named after INI sections
    (e.g. "TIME-RELATED_CONSTANTS", "INITITIAL CONDITIONS").
    Each entity class has a single entity with the same name as the class.
    Parameters are stored as string values.

    Parameters
    ----------
    db_url : str
        Spine database URL for the CWatM database.
    params : dict
        Parameter name -> value mapping (only those in PARAM_SECTIONS).

    Returns
    -------
    set of parameter names that were updated.
    """
    from spinedb_api import DatabaseMapping, to_database, Duration, DateTime

    url = str(db_url)
    if "://" not in url:
        url = f"sqlite:///{url}"

    updated = set()

    with DatabaseMapping(url) as db_map:
        db_map.fetch_all("entity")
        db_map.fetch_all("parameter_value")

        for param_name, param_value in params.items():
            entity_class = PARAM_SECTIONS.get(param_name)
            if not entity_class:
                continue
            
            if param_name == "loopcount":
                if param_value == "False":
                    param_value = False
                elif param_value == "True":
                    param_value = True
                else:
                    print(f"Unexpected value for loopcount: {param_value!r}")
                    print("Expected 'True' or 'False' for loopcount.")
            elif param_name == "initLoad":
                # Keep as string
                pass
            elif param_name == "RollFlexTool":
                param_value = Duration(param_value)
            else:
                param_value = DateTime(param_value)
            value, value_type = to_database(param_value)

            # Try to find existing parameter value across all alternatives
            existing = None
            for pval in db_map.find_parameter_values(
                entity_class_name=entity_class,
                parameter_definition_name=param_name,
            ):
                existing = pval

                if existing["alternative_name"] == "Coupling" or (existing["alternative_name"] == "Base" and param_name == "PathOut"):
                    # Update existing value
                    item = db_map.get_parameter_value_item(
                        entity_class_name=entity_class,
                        entity_byname=(entity_class,),
                        parameter_definition_name=param_name,
                        alternative_name=existing["alternative_name"],
                    )
                    if item:
                        item.update(value=value, type=value_type)
                        updated.add(param_name)
                        print(f"  [CWatM DB] Updated {param_name} = {param_value} "
                            f"(alt: {existing['alternative_name']})")

        try:
            db_map.commit_session("Updated time parameters from reset_rolling.py")
        except NothingToCommit:
            pass

    return updated


def update_rolling_solve_jump(db_url, jump_hours):
    """
    Write rolling_solve_jump to all solve entities in the FlexTool database.

    Parameters
    ----------
    db_url : str
        Spine database URL (e.g. sqlite:///path/to/db.sqlite)
    jump_hours : float
        Rolling solve jump in hours.
    """
    from spinedb_api import DatabaseMapping, to_database

    url = str(db_url)
    if "://" not in url:
        url = f"sqlite:///{url}"

    value, value_type = to_database(jump_hours)

    with DatabaseMapping(url) as db_map:
        db_map.fetch_all("entity")
        db_map.fetch_all("parameter_value")


        # Check if rolling_solve_jump already exists for this solve
        existing = None
        for pval in db_map.find_parameter_values(
            entity_class_name="solve",
            parameter_definition_name="rolling_solve_jump",
        ):
            existing = pval
            break

        if existing:

            db_map.add_or_update_parameter_value(
                entity_class_name="solve",
                entity_byname=existing['entity_byname'],
                parameter_definition_name="rolling_solve_jump",
                alternative_name=existing['alternative_name'],
                value=value,
                type=value_type,
            )
            print(f"  [DB] Updated rolling_solve_jump={jump_hours}h "
                    f"for solve '{existing['entity_name']}'")
        else:
            print("Cannot find a rolling_solve_jump parameter for any solve entity")
            print("Check that you have set the rolling parameters rolling_solve_jump and rolling_solve_horizon in the FlexTool database")
            print("Additionally, check that the nested storage solving or other storage usage limits exist to prevent storage emptying")
            sys.exit(-1)
        try:
            db_map.commit_session("Updated rolling_solve_jump from reset_rolling.py")
        except NothingToCommit:
            pass


def main():
    if len(sys.argv) != 4:
        print("Usage: python reset_rolling.py <time_params.txt> <cwatm_db> <flextool_db>")
        sys.exit(1)

    txt_path = sys.argv[1]
    cwatm_db = sys.argv[2]
    flextool_db = sys.argv[3]

    if not Path(txt_path).exists():
        print(f"ERROR: Text file not found: {txt_path}")
        sys.exit(1)

    params = read_params(txt_path)
    if not params:
        print("ERROR: No valid parameters found in text file")
        sys.exit(1)

    print(f"Parameters to set:")
    for k, v in params.items():
        print(f"  {k} = {v}  (entity class: {PARAM_SECTIONS[k]})")

    # Write time parameters to CWatM database
    print(f"\nUpdating CWatM database: {cwatm_db}")
    updated = update_cwatm_db(cwatm_db, params)

    for key in params:
        if key not in updated:
            print(f"  [WARN] {key} could not be updated")

    # Write RollFlexTool as rolling_solve_jump to FlexTool database
    if "RollFlexTool" in params:
        jump_hours = roll_to_hours(params["RollFlexTool"])
        print(f"\nRollFlexTool = {params['RollFlexTool']} -> {jump_hours} hours")
        print(f"Updating FlexTool database: {flextool_db}")
        update_rolling_solve_jump(flextool_db, jump_hours)
    else:
        print("\n  No RollFlexTool in text file, skipping FlexTool DB update")


if __name__ == "__main__":
    main()
