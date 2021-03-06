import configparser
import difflib
import logging
import os
from pathlib import Path

import pytoml as toml

from .validate import validate_config
from .vendorized.readme.rst import render
import io

log = logging.getLogger(__name__)

class ConfigError(ValueError):
    pass

metadata_list_fields = {
    'classifiers',
    'requires',
    'dev-requires'
}

metadata_allowed_fields = {
    'module',
    'author',
    'author-email',
    'maintainer',
    'maintainer-email',
    'home-page',
    'license',
    'keywords',
    'requires-python',
    'dist-name',
    'entry-points-file',
    'description-file',
} | metadata_list_fields

metadata_required_fields = {
    'module',
    'author',
    'author-email',
    'home-page',
}


def read_pkg_ini(path: Path):
    """Read and check the `pyproject.toml` or `flit.ini` file with data about the package.
    """
    if path.suffix == '.toml':
        with path.open() as f:
            d = toml.load(f)
        res = prep_toml_config(d, path)
    else:
        # Treat all other extensions as the older flit.ini format
        cp = _read_pkg_ini(path)
        res = _validate_config(cp, path)

    if validate_config(res):
        if os.environ.get('FLIT_ALLOW_INVALID'):
            log.warning("Allowing invalid data (FLIT_ALLOW_INVALID set). Uploads may still fail.")
        else:
            raise ConfigError("Invalid config values (see log)")
    return res

class EntryPointsConflict(ConfigError):
    def __str__(self):
        return ('Please specify console_scripts entry points, or [scripts] in '
            'flit config, not both.')

def prep_toml_config(d, path):
    """Validate config loaded from pyproject.toml and prepare common metadata
    
    Returns a dictionary with keys: module, metadata, scripts, entrypoints,
    raw_config.
    """
    if ('tool' not in d) or ('flit' not in d['tool']) \
            or (not isinstance(d['tool']['flit'], dict)):
        raise ConfigError("TOML file missing [tool.flit] table.")

    d = d['tool']['flit']
    unknown_sections = set(d) - {'metadata', 'scripts', 'entrypoints'}
    unknown_sections = [s for s in unknown_sections if not s.lower().startswith('x-')]
    if unknown_sections:
        raise ConfigError('Unknown sections: ' + ', '.join(unknown_sections))

    if 'metadata' not in d:
        raise ConfigError('[tool.flit.metadata] section is required')

    md_dict, module = _prep_metadata(d['metadata'], path)

    if 'scripts' in d:
        scripts_dict = dict(d['scripts'])
    else:
        scripts_dict = {}

    if 'entrypoints' in d:
        entrypoints = flatten_entrypoints(d['entrypoints'])
    else:
        entrypoints = {}
    _add_scripts_to_entrypoints(entrypoints, scripts_dict)

    return {
        'module': module,
        'metadata': md_dict,
        'scripts': scripts_dict,
        'entrypoints': entrypoints,
        'raw_config': d,
    }

def flatten_entrypoints(ep):
    """Flatten nested entrypoints dicts.

    Entry points group names can include dots. But dots in TOML make nested
    dictionaries:

    [entrypoints.a.b]    # {'entrypoints': {'a': {'b': {}}}}

    The proper way to avoid this is:

    [entrypoints."a.b"]  # {'entrypoints': {'a.b': {}}}

    But since there isn't a need for arbitrarily nested mappings in entrypoints,
    flit allows you to use the former. This flattens the nested dictionaries
    from loading pyproject.toml.
    """
    def _flatten(d, prefix):
        d1 = {}
        for k, v in d.items():
            if isinstance(v, dict):
                yield from _flatten(v, prefix+'.'+k)
            else:
                d1[k] = v

        if d1:
            yield prefix, d1

    res = {}
    for k, v in ep.items():
        res.update(_flatten(v, k))
    return res

def _add_scripts_to_entrypoints(entrypoints, scripts_dict):
    if scripts_dict:
        if 'console_scripts' in entrypoints:
            raise EntryPointsConflict
        else:
            entrypoints['console_scripts'] = scripts_dict


def _read_pkg_ini(path):
    """Reads old-style flit.ini
    """
    cp = configparser.ConfigParser()
    with path.open(encoding='utf-8') as f:
        cp.read_file(f)

    return cp

def _prep_metadata(md_sect, path):
    """Process & verify the metadata from a config file
    
    - Pull out the module name we're packaging.
    - Read description-file and check that it's valid rst
    - Convert dashes in key names to underscores
      (e.g. home-page in config -> home_page in metadata) 
    """
    if not set(md_sect).issuperset(metadata_required_fields):
        missing = metadata_required_fields - set(md_sect)
        raise ConfigError("Required fields missing: " + '\n'.join(missing))

    module = md_sect.get('module')
    if not module.isidentifier():
        raise ConfigError("Module name %r is not a valid identifier" % module)

    md_dict = {}

    # Description file
    if 'description-file' in md_sect:
        description_file = path.parent / md_sect.get('description-file')
        with description_file.open(encoding='utf-8') as f:
            raw_desc =  f.read()
        if description_file.suffix == '.md':
            try:
                import pypandoc
                log.debug('will convert %s to rst', description_file)
                raw_desc = pypandoc.convert(raw_desc, 'rst', format='markdown')
            except Exception:
                log.warning('Unable to convert markdown to rst. Please install '
                    '`pypandoc` and `pandoc` to use markdown long description.')

        # rst check
        stream = io.StringIO()
        res = render(raw_desc, stream)
        if not res:
            log.warning("The file description seems not to be valid rst for PyPI;"
                    " it will be interpreted as plain text")
            log.warning(stream.getvalue())
        md_dict['description'] =  raw_desc

    for key, value in md_sect.items():
        if key in {'description-file', 'module'}:
            continue
        if key not in metadata_allowed_fields:
            closest = difflib.get_close_matches(key, metadata_allowed_fields,
                                                n=1, cutoff=0.7)
            msg = "Unrecognised metadata key: {!r}".format(key)
            if closest:
                msg += " (did you mean {!r}?)".format(closest[0])
            raise ConfigError(msg)

        k2 = key.replace('-', '_')
        md_dict[k2] = value
        if key in metadata_list_fields:
            if not isinstance(value, list):
                raise ConfigError('Expected a list for {} field, found {!r}'
                                    .format(key, value))
            if not all(isinstance(a, str) for a in value):
                raise ConfigError('Expected a list of strings for {} field'
                                    .format(key))
        else:
            if not isinstance(value, str):
                raise ConfigError('Expected a string for {} field, found {!r}'
                                    .format(key, value))

    # What we call requires in the ini file is technically requires_dist in
    # the metadata.
    if 'requires' in md_dict:
        md_dict['requires_dist'] = md_dict.pop('requires')

    # And what we call dist-name is name in the metadata
    if 'dist_name' in md_dict:
        md_dict['name'] = md_dict.pop('dist_name')

    return md_dict, module

def _validate_config(cp, path):
    """Validate and process config loaded from a flit.ini file.
    
    Returns a dict with keys: module, metadata, scripts, entrypoints, raw_config
    """
    unknown_sections = set(cp.sections()) - {'metadata', 'scripts'}
    unknown_sections = [s for s in unknown_sections if not s.lower().startswith('x-')]
    if unknown_sections:
        raise ConfigError('Unknown sections: ' + ', '.join(unknown_sections))

    if not cp.has_section('metadata'):
        raise ConfigError('[metadata] section is required')

    md_sect = {}
    for k, v in cp['metadata'].items():
        if k in metadata_list_fields:
            md_sect[k] = [l for l in v.splitlines() if l.strip()]
        else:
            md_sect[k] = v

    if 'entry-points-file' in md_sect:
        entry_points_file = path.parent / md_sect.pop('entry-points-file')
        if not entry_points_file.is_file():
            raise FileNotFoundError(entry_points_file)
    else:
        entry_points_file = path.parent / 'entry_points.txt'
        if not entry_points_file.is_file():
            entry_points_file = None

    if entry_points_file:
        ep_cp = configparser.ConfigParser()
        with entry_points_file.open() as f:
            ep_cp.read_file(f)
        # Convert to regular dict
        entrypoints = {k: dict(v) for k,v in ep_cp.items()}
    else:
        entrypoints = {}

    md_dict, module = _prep_metadata(md_sect, path)

    # Scripts ---------------
    if cp.has_section('scripts'):
        scripts_dict = dict(cp['scripts'])
    else:
        scripts_dict = {}

    _add_scripts_to_entrypoints(entrypoints, scripts_dict)

    return {
        'module': module,
        'metadata': md_dict,
        'scripts': scripts_dict,
        'entrypoints': entrypoints,
        'raw_config': cp,
    }
