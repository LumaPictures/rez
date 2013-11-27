"""
rez packages
"""
import os.path
import re
import sys
from public_enums import PKG_METADATA_FILENAME
from rez_metafile import iter_resources, load_metadata
import rez_filesys
from rez_exceptions import PkgSystemError
from versions import Version, ExactVersion, VersionRange, ExactVersionSet

PACKAGE_NAME_REGSTR = '[a-zA-Z][a-zA-Z0-9_]*'
PACKAGE_NAME_REGEX = re.compile(PACKAGE_NAME_REGSTR + '$')

def split_name(pkg_str):
    strs = pkg_str.split('-')
    if len(strs) > 2:
        PkgSystemError("Invalid package string '" + pkg_str + "'")
    name = strs[0]
    if len(strs) == 1:
        verrange = ""
    else:
        verrange = strs[1]
    return name, verrange

def pkg_name(pkg_str):
    return split_name(pkg_str)[0]

def iter_package_families(name=None, paths=None):
    """
    Iterate through top-level `PackageFamily` instances.
    """
    if paths is None:
        paths = rez_filesys._g_syspaths
    elif isinstance(paths, basestring):
        paths = [paths]

    pkg_iter = iter_resources(0,  # configuration version
                              ['package_family.external', 'package_family.folder'],
                              paths,
                              name=name)
    for path, variables, resource_info in pkg_iter:
        if os.path.isdir(path):
            yield PackageFamily(variables['name'], path)
        else:
            yield ExternalPackageFamily(variables['name'], path)

def package_family(name, paths=None):
    """
    Return the first `FamilyPackage` found on the search path.
    """
    result = iter_package_families(name, paths)
    try:
        # return first item in generator
        return next(result)
    except StopIteration:
        return None

def iter_packages(name=None, paths=None, skip_dupes=True):
    """
    Iterate through all packages
    """
    done = set()
    for pkg_fam in iter_package_families(name, paths):
        for pkg in pkg_fam.iter_version_packages():
            if skip_dupes:
                if pkg.short_name() not in done:
                    done.add(pkg.short_name())
                    yield pkg
            else:
                yield pkg

def iter_packages_in_range(family_name, ver_range, latest=True, timestamp=0,
                           exact=False, paths=None):
    """
    Iterate over `Package` instances.

    Parameters
    ----------
    family_name : str
        name of the package without a version
    ver_range : VersionRange
        range of versions in package to iterate over
    latest : bool
        whether to sort by latest (default) or earliest
    timestamp : int
        time since epoch: any packages newer than this will be ignored. 0 means
        no effect.
    exact : bool
        only match if ver_range represents an exact version
    paths : list of str
        search path. defaults to REZ_PACKAGES_PATH

    If two versions in two different paths are the same, then the package in
    the first path is returned in preference.
    """
    if not isinstance(ver_range, VersionRange):
        ver_range = VersionRange(ver_range)

    # store the generator. no paths have been walked yet
    results = iter_packages(family_name, paths)

    if timestamp:
        results = [x for x in results if x.timestamp <= timestamp]
    # sort
    if latest:
        results = sorted(results, key=lambda x: x.version, reverse=True)
    else:
        results = sorted(results, key=lambda x: x.version, reverse=False)

    # find the best match, skipping dupes
    for result in results:
        if ver_range.matches_version(result.version, allow_inexact=not exact):
            yield result

def package_in_range(family_name, ver_range, latest=True, timestamp=0,
                     exact=False, paths=None):
    """
    Return the first `Package` found on the search path.

    Parameters
    ----------
    family_name : str
        name of the package without a version
    ver_range : VersionRange
        range of versions in package to iterate over
    latest : bool
        whether to sort by latest (default) or earliest
    timestamp : int
        time since epoch: any packages newer than this will be ignored. 0 means
        no effect.
    exact : bool
        only match if ver_range represents an exact version
    paths : list of str
        search path. defaults to REZ_PACKAGES_PATH

    """
    result = iter_packages_in_range(family_name, ver_range, latest, timestamp,
                                    exact, paths)
    try:
        # return first item in generator
        return next(result)
    except StopIteration:
        return None

class PackageFamily(object):
    """
    A package family has a single root directory, with a sub-directory for each
    version.
    """
    def __init__(self, name, path):
        self.name = name
        self.path = path

    def iter_version_packages(self):
        pkg_iter = iter_resources(0,  # configuration version
                                  ['package.versionless', 'package.versioned'],
                                  [os.path.dirname(self.path)],
                                  name=self.name)
        for metafile, variables, resource_info in pkg_iter:
            yield Package(self.name, ExactVersion(variables.get('version', '')),
                          metafile)

class ExternalPackageFamily(PackageFamily):
    """
    special case where the entire package is stored in one file
    """
    def __init__(self, name, path):
        PackageFamily.__init__(self, name, path)
        self._metadata = None

    @property
    def raw_metadata(self):
        # bypass the memcache so that non-essentials are not stripped
        if self._metadata is None:
            self._metadata = load_metadata(self.path)
            if isinstance(self._metadata, list):
                family_data = self._metadata[0]
                # copy data from main metadata into sub-sections
                for ver_data in self._metadata[1:]:
                    # only set data from family package that does not exist in version package
                    for key, value in family_data.iteritems():
                        ver_data.setdefault(key, value)
        return self._metadata

    @property
    def metadata(self):
        if isinstance(self.raw_metadata, list):
            return self.raw_metadata[0]
        else:
            return self.raw_metadata

    @property
    def sub_metadata(self):
        if isinstance(self.raw_metadata, list):
            return self.raw_metadata[1:]
        else:
            return []

    @property
    def versions(self):
        versions = self.metadata['versions']
        if not versions:
            return ExactVersionSet([])
        return ExactVersionSet(versions)

    def iter_version_packages(self):
        versions = self.versions
        if versions.is_none():
            yield Package(self.name, ExactVersion(''),
                          self.path,
                          0,
                          metadata=self.metadata,
                          stripped_metadata=self.metadata)
        else:
            for version in self.versions:
                data = self.metadata
                for ver_data in self.sub_metadata:
                    if version in VersionRange(ver_data['version']):
                        data = ver_data.copy()
                        break
                data['version'] = version
                yield Package(self.name, version,
                              self.path,
                              0,
                              metadata=data,
                              stripped_metadata=data)

class Package(object):
    """
    an unresolved package.

    An unresolved package has a version, but may have inexplicit or "variant"
    requirements, which can only be determined once its co-packages
    are known. When the exact list of requirements is determined, the package
    is considered resolved and the full path to the package root is known.
    """
    def __init__(self, name, version, path, timestamp=None, metadata=None,
                 stripped_metadata=None):
        self.name = name
        self.version = version
        assert os.path.splitext(path)[1], "%s: %s" % (self.name, path)
        self.base = os.path.dirname(path)
        self.metafile = path
        self._timestamp = timestamp
        self._metadata = metadata
        self._stripped_metdata = stripped_metadata

    @property
    def metadata(self):
        # bypass the memcache so that non-essentials are not stripped
        if self._metadata is None:
            self._metadata = load_metadata(self.metafile)
        return self._metadata

    @property
    def stripped_metadata(self):
        """
        only the essential metadata
        """
        if self._stripped_metdata is None:
            from rez_memcached import get_memcache
            self._stripped_metdata = get_memcache().get_metafile(self.metafile)
        return self._stripped_metdata

    @property
    def timestamp(self):
        if self._timestamp is None:
            self._timestamp = 0
            if not self.is_local():
                # TODO: replace this with package resources
                release_time_f = os.path.join(self.base, '.metadata', 'release_time.txt')
                if os.path.isfile(release_time_f):
                    with open(release_time_f, 'r') as f:
                        self._timestamp = int(f.read().strip())
                elif rez_filesys._g_new_timestamp_behaviour:
                    s = ("Warning: The package at %s is not timestamped and will be ignored. " +
                         "To timestamp it manually, use the rez-timestamp utility.")
                    print >> sys.stderr, s % self.base
        return self._timestamp

    def short_name(self):
        if (len(str(self.version)) == 0):
            return self.name
        else:
            return self.name + '-' + str(self.version)

    def is_local(self):
        return self.base.startswith(rez_filesys._g_local_pkgs_path)

    def __str__(self):
        return str([self.name, self.version, self.base])

    def __repr__(self):
        return "%s(%r, %r)" % (self.__class__.__name__, self.name,
                               self.version)

class ResolvedPackage(Package):
    """
    A resolved package.

    An unresolved package has a version, but may have inexplicit or "variant"
    requirements, which can only be determined once its co-packages
    are known. When the exact list of requirements is determined, the package
    is considered resolved and the full path to the package root is known.
    """
    def __init__(self, name, version, base, root, commands, metadata, timestamp, metafile):
        Package.__init__(self, name, version, metafile, timestamp, stripped_metadata=metadata)
        self.version = ExactVersion(version)
        self.base = base
        self.root = root
        self.raw_commands = commands
        self.commands = None
        self.metafile = metafile

    def strip(self):
        # remove data that we don't want to cache
        self.commands = None
        self.raw_commands = None
        self._metadata = None

    def __str__(self):
        return str([self.name, self.version, self.root])

    def __repr__(self):
        return "%s(%r, %r, %r)" % (self.__class__.__name__, self.name,
                                   self.version, self.root)

# metafile_to_package_class = {rez_metafile.BasePackageConfig_0: Package,
#                              rez_metafile.VersionPackageConfig_0: Package,
#                              rez_metafile.ExternalPackageConfigList_0: ExternalPackageFamily,
#                              rez_metafile.ExternalPackageConfig_0: ExternalPackageFamily,
#                             }
