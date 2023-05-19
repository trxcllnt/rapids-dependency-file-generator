import itertools
import os
import re
import sys
import textwrap
from collections import defaultdict

import tomlkit
import yaml

from .constants import (
    OutputTypes,
    cli_name,
    default_channels,
    default_conda_dir,
    default_pyproject_dir,
    default_requirements_dir,
)

OUTPUT_ENUM_VALUES = [str(x) for x in OutputTypes]
NON_NONE_OUTPUT_ENUM_VALUES = [str(x) for x in OutputTypes if not x == OutputTypes.NONE]
HEADER = f"# This file is generated by `{cli_name}`."


def delete_existing_files(root="."):
    """Delete any files generated by this generator.

    This function can be used to clean up a directory tree before generating a new set
    of files from scratch.

    Parameters
    ----------
    root : str
        The path to the root of the directory tree to search for files to delete.
    """
    for dirpath, _, filenames in os.walk(root):
        for fn in filter(
            lambda fn: fn.endswith(".txt") or fn.endswith(".yaml"), filenames
        ):
            with open(file_path := os.path.join(dirpath, fn)) as f:
                if HEADER in f.read():
                    os.remove(file_path)


def dedupe(dependencies):
    """Generate the unique set of dependencies contained in a dependency list.

    Parameters
    ----------
    dependencies : Sequence
        A sequence containing dependencies (possibly including duplicates).

    Yields
    ------
    list
        The `dependencies` with all duplicates removed.
    """
    deduped = sorted({dep for dep in dependencies if not isinstance(dep, dict)})
    dict_deps = defaultdict(list)
    for dep in filter(lambda dep: isinstance(dep, dict), dependencies):
        for key, values in dep.items():
            dict_deps[key].extend(values)
            dict_deps[key] = sorted(set(dict_deps[key]))
    if dict_deps:
        deduped.append(dict(dict_deps))
    return deduped


def grid(gridspec):
    """Yields the Cartesian product of a `dict` of iterables.

    The input ``gridspec`` is a dictionary whose keys correspond to
    parameter names. Each key is associated with an iterable of the
    values that parameter could take on. The result is a sequence of
    dictionaries where each dictionary has one of the unique combinations
    of the parameter values.

    Parameters
    ----------
    gridspec : dict
        A mapping from parameter names to lists of parameter values.

    Yields
    ------
    Iterable[dict]
        Each yielded value is a dictionary containing one of the unique
        combinations of parameter values from `gridspec`.
    """
    for values in itertools.product(*gridspec.values()):
        yield dict(zip(gridspec.keys(), values))


def make_dependency_file(
    file_contents,
    file_type,
    name,
    config_file,
    output_dir,
    conda_channels,
    dependencies,
    extras=None,
):
    """Generate the contents of the dependency file.

    Parameters
    ----------
    file_type : str
        A string corresponding to the value of a member of constants.OutputTypes.
    name : str
        The name of the file to write.
    config_file : str
        The full path to the dependencies.yaml file.
    output_dir : str
        The path to the directory where the dependency files will be written.
    conda_channels : str
        The channels to include in the file. Only used when `file_type` is
        CONDA.
    dependencies : list
        The dependencies to include in the file.
    extras : dict
        Any extra information provided for generating this dependency file.

    Returns
    -------
    str
        The contents of the file.
    """

    if file_type == str(OutputTypes.CONDA):
        data = yaml.load(file_contents, Loader=yaml.FullLoader) or {}
        data["name"] = os.path.splitext(name)[0]
        data.setdefault("channels", []).extend(conda_channels)
        data.setdefault("dependencies", []).extend(dependencies)
        file_contents = yaml.dump(data)
    elif file_type == str(OutputTypes.REQUIREMENTS):
        file_contents += "\n".join(dependencies) + "\n"
    elif file_type == str(OutputTypes.PYPROJECT):
        toml_deps = tomlkit.array()
        for dep in dependencies:
            toml_deps.add_line(dep)
        toml_deps.add_line(indent="")

        # Recursively descend into subtables like "[x.y.z]", creating tables as needed.
        table = file_contents
        for section in extras["table"].split("."):
            try:
                table = table[section]
            except tomlkit.exceptions.NonExistentKey:
                # If table is not a super-table (i.e. if it has its own contents and is
                # not simply parted of a nested table name 'x.y.z') add a new line
                # before adding a new sub-table.
                if not table.is_super_table():
                    table.add(tomlkit.nl())
                table[section] = tomlkit.table()
                table = table[section]

        key = extras.get(
            "key", "requires" if extras["table"] == "build-system" else "dependencies"
        )
        table[key] = toml_deps

    return file_contents


def get_requested_output_types(output):
    """Get the list of output file types to generate.

    The possible options are enumerated by `constants.OutputTypes`. If the only
    requested output is `NONE`, returns an empty list.

    Parameters
    ----------
    output : str or List[str]
        A string or list of strings indicating the output types.

    Returns
    -------
    List[str]
        The list of output file types to generate.

    Raises
    -------
    ValueError
        If multiple outputs are requested and one of them is NONE, or if an
        unknown output type is requested.
    """
    output = output if isinstance(output, list) else [output]

    if output == [str(OutputTypes.NONE)]:
        return []

    if len(output) > 1 and str(OutputTypes.NONE) in output:
        raise ValueError("'output: [none]' cannot be combined with any other values.")

    if any(v not in NON_NONE_OUTPUT_ENUM_VALUES for v in output):
        raise ValueError(
            "'output' key can only be "
            + ", ".join(f"'{x}'" for x in OUTPUT_ENUM_VALUES)
            + f" or a list of the non-'{OutputTypes.NONE}' values."
        )
    return output


def get_filename(file_type, file_key, matrix_combo):
    """Get the name of the file to which to write a generated dependency set.

    The file name will be composed of the following components, each determined
    by the `file_type`:
        - A file-type-based prefix e.g. "requirements" for requirements.txt files.
        - A name determined by the value of $FILENAME in the corresponding
          [files.$FILENAME] section of dependencies.yaml. This name is used for some
          output types (conda, requirements) and not others (pyproject).
        - A matrix description encoding the key-value pairs in `matrix_combo`.
        - A suitable extension for the file (e.g. ".yaml" for conda environment files.)

    Parameters
    ----------
    file_type : str
        A string corresponding to the value of a member of constants.OutputTypes.
    file_prefix : str
        The name of this member in the [files] list in dependencies.yaml.
    matrix_combo : dict
        A mapping of key-value pairs corresponding to the
        [files.$FILENAME.matrix] entry in dependencies.yaml.

    Returns
    -------
    str
        The name of the file to generate.
    """
    file_type_prefix = ""
    file_ext = ""
    file_name_prefix = file_key
    suffix = "_".join([f"{k}-{v}" for k, v in matrix_combo.items()])
    if file_type == str(OutputTypes.CONDA):
        file_ext = ".yaml"
    elif file_type == str(OutputTypes.REQUIREMENTS):
        file_ext = ".txt"
        file_type_prefix = "requirements"
    elif file_type == str(OutputTypes.PYPROJECT):
        file_ext = ".toml"
        # Unlike for files like requirements.txt or conda environment YAML files, which
        # may be named with additional prefixes (e.g. all_cuda_*) pyproject.toml files
        # need to have that exact name and are never prefixed.
        suffix = ""
        file_name_prefix = str(OutputTypes.PYPROJECT)
    filename = "_".join(
        filter(None, (file_type_prefix, file_name_prefix, suffix))
    ).replace(".", "")
    return filename + file_ext


def get_output_dir(file_type, config_file_path, file_config):
    """Get the directory to which to write a generated dependency file.

    The output directory is determined by the `file_type` and the corresponding
    key in the `file_config`. The path provided in `file_config` will be taken
    relative to `config_file_path`.

    Parameters
    ----------
    file_type : str
        A string corresponding to the value of a member of constants.OutputTypes.
    config_file_path : str
        The path to the dependencies.yaml file.
    file_config : dict
        A dictionary corresponding to one of the [files.$FILENAME] sections of
        the dependencies.yaml file. May contain `conda_dir` or
        `requirements_dir`.

    Returns
    -------
    str
        The directory to write the file to.
    """
    path = [os.path.dirname(config_file_path)]
    if file_type == str(OutputTypes.CONDA):
        path.append(file_config.get("conda_dir", default_conda_dir))
    elif file_type == str(OutputTypes.REQUIREMENTS):
        path.append(file_config.get("requirements_dir", default_requirements_dir))
    elif file_type == str(OutputTypes.PYPROJECT):
        path.append(file_config.get("pyproject_dir", default_pyproject_dir))
    return os.path.join(*path)


def should_use_specific_entry(matrix_combo, specific_entry_matrix):
    """Check if an entry should be used.

    Dependencies listed in the [dependencies.$DEPENDENCY_GROUP.specific]
    section are specific to a particular matrix entry provided by the
    [matrices] list. This function validates the [matrices.matrix] value
    against the provided `matrix_combo` to check if they are compatible.

    A `specific_entry_matrix` is compatible with a `matrix_combo` if and only if
    `specific_entry_matrix[key] == matrix_combo[key]` for every key defined in
    `specific_entry_matrix`. A `matrix_combo` may contain additional keys not
    specified by `specific_entry_matrix`.

    Parameters
    ----------
    matrix_combo : dict
        A mapping from matrix keys to values for the current file being
        generated.
    specific_entry_matrix : dict
        A mapping from matrix keys to values for the current specific
        dependency set being checked.

    Returns
    -------
    bool
        True if the `specific_entry_matrix` is compatible with the current
        `matrix_combo` and False otherwise.
    """
    return all(
        matrix_combo.get(specific_key) == specific_value
        for specific_key, specific_value in specific_entry_matrix.items()
    )


def name_with_cuda_suffix(name, cuda_version=None, cuda_suffix="-cu"):
    """Appends the CUDA major version suffix to the package name.

    Finds and removes existing CUDA version suffix if present.

    Parameters
    ----------
    name : str
        The pyproject.toml's [package.name] field
    cuda_version : str or None
        The CUDA version. Defaults to None.
    cuda_suffix : str
        The string to use as the CUDA version suffix in the package name.
        The major version of the matrix's CUDA axis (if given) is appended
        to this string. Defaults to ```-cu```.
    """
    # Find and remove existing `-cuXX` suffix if present
    suff = re.search("(" + cuda_suffix + "[0-9]{2})$", name)
    name = name[0 : suff.span(0)[0] if suff else len(name)]
    # Append `-cuXX` suffix to `[package.name]`
    if cuda_version is not None:
        name += cuda_suffix + cuda_version.split(".")[0]
    return name


def make_dependency_files(
    parsed_config, config_file_path, to_stdout, cuda_suffix="-cu"
):
    """Generate dependency files.

    This function iterates over data parsed from a YAML file conforming to the
    `dependencies.yaml file spec <https://github.com/rapidsai/dependency-file-generator#dependenciesyaml-format>__`
    and produces the requested files.

    Parameters
    ----------
    parsed_config : dict
       The parsed dependencies.yaml config file.
    config_file_path : str
        The path to the dependencies.yaml file.
    to_stdout : bool
        Whether the output should be written to stdout. If False, it will be
        written to a file computed based on the output file type and
        config_file_path.
    cuda_suffix : str
        The string to use as the CUDA version suffix in the package name.
        The major version of the matrix's CUDA axis (if given) is appended
        to this string. Defaults to ```-cu```.

    Raises
    -------
    ValueError
        If the file is malformed. There are numerous different error cases
        which are described by the error messages.
    """

    channels = parsed_config.get("channels", default_channels) or default_channels
    files = parsed_config["files"]
    results = {}

    for file_key, file_config in files.items():
        includes = file_config["includes"]

        file_types_to_generate = get_requested_output_types(file_config["output"])

        extras = file_config.get("extras", {})

        for file_type in file_types_to_generate:
            for matrix_combo in grid(file_config.get("matrix", {})):
                dependencies = []

                # Collect all includes from each dependency list corresponding
                # to this (file_name, file_type, matrix_combo) tuple. The
                # current tuple corresponds to a single file to be written.
                for include in includes:
                    dependency_entry = parsed_config["dependencies"][include]

                    for common_entry in dependency_entry.get("common", []):
                        if file_type not in common_entry["output_types"]:
                            continue
                        dependencies.extend(common_entry["packages"])

                    for specific_entry in dependency_entry.get("specific", []):
                        if file_type not in specific_entry["output_types"]:
                            continue

                        found = False
                        fallback_entry = None
                        for specific_matrices_entry in specific_entry["matrices"]:
                            # An empty `specific_matrices_entry["matrix"]` is
                            # valid and can be used to specify a fallback_entry for a
                            # `matrix_combo` for which no specific entry
                            # exists. In that case we save the fallback_entry result
                            # and only use it at the end if nothing more
                            # specific is found.
                            if not specific_matrices_entry.get("matrix", None):
                                fallback_entry = specific_matrices_entry
                                continue

                            if should_use_specific_entry(
                                matrix_combo, specific_matrices_entry["matrix"]
                            ):
                                # Raise an error if multiple specific entries
                                # (not including the fallback_entry) match a
                                # requested matrix combination.
                                if found:
                                    raise ValueError(
                                        f"Found multiple matches for matrix {matrix_combo}"
                                    )
                                found = True
                                # A package list may be empty as a way to
                                # indicate that for some matrix elements no
                                # packages should be installed.
                                dependencies.extend(
                                    specific_matrices_entry.get("packages", []) or []
                                )

                        if not found:
                            if fallback_entry is not None:
                                dependencies.extend(
                                    fallback_entry.get("packages", []) or []
                                )
                            else:
                                raise ValueError(
                                    f"No matching matrix found in '{include}' for: {matrix_combo}"
                                )

                # Dedupe deps and print / write to filesystem
                full_file_name = get_filename(file_type, file_key, matrix_combo)

                output_dir = get_output_dir(file_type, config_file_path, file_config)

                output_file_path = os.path.join(output_dir, full_file_name)

                if results.get(output_file_path, None) is None:
                    if file_type != str(OutputTypes.PYPROJECT):
                        results[output_file_path] = ""
                    else:
                        # pyproject.toml needs to be modified in place instead of built from scratch.
                        with open(output_file_path) as f:
                            results[output_file_path] = tomlkit.load(f)
                            # Append `-cuXX` to `[package.name]`
                            results[output_file_path]["project"][
                                "name"
                            ] = name_with_cuda_suffix(
                                results[output_file_path]["project"]["name"],
                                matrix_combo.get("cuda", None),
                                cuda_suffix,
                            )

                results[output_file_path] = make_dependency_file(
                    results[output_file_path],
                    file_type,
                    full_file_name,
                    config_file_path,
                    output_dir,
                    channels,
                    dedupe(dependencies),
                    extras,
                )

    def write_output(data, path, f):

        output_dir = os.path.dirname(path)
        relpath_to_config_file = os.path.relpath(config_file_path, output_dir)
        header = textwrap.dedent(
            f"""\
            {HEADER}
            # To make changes, edit {relpath_to_config_file} and run `{cli_name}`.
            """
        )

        if isinstance(data, dict):
            header = header.strip()
            first_2_lines = "\n".join(data.as_string().split("\n")[0:2])
            if first_2_lines != header:
                data.body[0:0] = [
                    (None, tomlkit.comment(line[2:])) for line in header.split("\n")
                ]
            tomlkit.dump(data, f)
        else:
            f.write(header)
            f.write(data)

    for path, data in results.items():
        if to_stdout:
            write_output(data, path, sys.stdout)
        else:
            with open(path, "w") as f:
                write_output(data, path, f)
