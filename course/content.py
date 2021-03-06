# -*- coding: utf-8 -*-

from __future__ import division

__copyright__ = "Copyright (C) 2014 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from django.conf import settings
from django.utils.translation import ugettext as _

import re
import datetime
import six
import sys

from django.utils.timezone import now
from django.core.exceptions import ObjectDoesNotExist, ImproperlyConfigured
from django.core.urlresolvers import NoReverseMatch

from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

from six.moves import html_parser

from jinja2 import (
        BaseLoader as BaseTemplateLoader, TemplateNotFound, FileSystemLoader)

from relate.utils import dict_to_struct

from yaml import load as load_yaml

if sys.version_info >= (3,):
    CACHE_KEY_ROOT = "py3"
else:
    CACHE_KEY_ROOT = "py2"


# {{{ repo blob getting

class SubdirRepoWrapper(object):
    def __init__(self, repo, subdir):
        self.repo = repo

        # This wrapper should only get used if there is a subdir to be had.
        assert subdir
        self.subdir = subdir

    def controldir(self):
        return self.repo.controldir()

    def close(self):
        self.repo.close()


def get_true_repo_and_path(repo, path):
    if isinstance(repo, SubdirRepoWrapper):
        if path:
            path = repo.subdir + "/" + path
        else:
            path = repo.subdir

        return repo.repo, path

    else:
        return repo, path


def get_course_repo_path(course):
    from os.path import join
    return join(settings.GIT_ROOT, course.identifier)


def get_course_repo(course):
    from dulwich.repo import Repo
    repo = Repo(get_course_repo_path(course))

    if course.course_root_path:
        return SubdirRepoWrapper(repo, course.course_root_path)
    else:
        return repo


def get_repo_blob(repo, full_name, commit_sha, allow_tree=True):
    """
    :arg full_name: A Unicode string indicating the file name.
    :arg commit_sha: A byte string containing the commit hash
    :arg allow_tree: Allow the resulting object to be a directory
    """

    repo, full_name = get_true_repo_and_path(repo, full_name)

    names = full_name.split("/")

    # Allow non-ASCII file name
    full_name = full_name.encode('utf-8')

    tree_sha = repo[commit_sha].tree
    tree = repo[tree_sha]

    def access_directory_content(maybe_tree, name):
        try:
            mode_and_blob_sha = tree[name.encode()]
        except TypeError:
            raise ObjectDoesNotExist(_("resource '%s' is a file, "
                "not a directory")
                % full_name.decode("utf-8"))

        mode, blob_sha = mode_and_blob_sha
        return mode_and_blob_sha

    if not full_name:
        if allow_tree:
            return tree
        else:
            raise ObjectDoesNotExist(
                    _("repo root is a directory, not a file"))

    try:
        for name in names[:-1]:
            if not name:
                # tolerate empty path components (begrudgingly)
                continue

            mode, blob_sha = access_directory_content(tree, name)
            tree = repo[blob_sha]

        mode, blob_sha = access_directory_content(tree, names[-1])

        result = repo[blob_sha]
        if not allow_tree and not hasattr(result, "data"):
            raise ObjectDoesNotExist(
                    _("resource '%s' is a directory, not a file")
                    % full_name)

        return result

    except KeyError:
        raise ObjectDoesNotExist(_("resource '%s' not found")
                % full_name.decode("utf-8"))


def get_repo_blob_data_cached(repo, full_name, commit_sha):
    """
    :arg commit_sha: A byte string containing the commit hash
    """

    if isinstance(commit_sha, six.binary_type):
        from six.moves.urllib.parse import quote_plus
        cache_key = "%s%R%1".join((
            CACHE_KEY_ROOT,
            quote_plus(repo.controldir()),
            quote_plus(full_name),
            commit_sha.decode(),
            ".".join(str(s) for s in sys.version_info[:2]),
            ))
    else:
        cache_key = None

    try:
        import django.core.cache as cache
    except ImproperlyConfigured:
        cache_key = None

    if cache_key is None:
        result = get_repo_blob(repo, full_name, commit_sha,
                allow_tree=False).data
        assert isinstance(result, six.binary_type)
        return result

    # Byte string is wrapped in a tuple to force pickling because memcache's
    # python wrapper appears to auto-decode/encode string values, thus trying
    # to decode our byte strings. Grr.

    def_cache = cache.caches["default"]

    result = None
    # Memcache is apparently limited to 250 characters.
    if len(cache_key) < 240:
        result = def_cache.get(cache_key)
    if result is not None:
        (result,) = result
        assert isinstance(result, six.binary_type), cache_key
        return result

    result = get_repo_blob(repo, full_name, commit_sha,
            allow_tree=False).data

    if len(result) <= getattr(settings, "RELATE_CACHE_MAX_BYTES", 0):
        def_cache.add(cache_key, (result,), None)

    assert isinstance(result, six.binary_type)

    return result


def is_repo_file_accessible_as(access_kind, repo, commit_sha, path):
    """
    Check of a file in a repo directory is accessible.  For example,
    'instructor' can access anything listed in the attributes.
    'student' can access 'student' and 'unenrolled'.  The 'unenrolled' role
    can only access 'unenrolled'.

    :arg commit_sha: A byte string containing the commit hash
    """

    # set the path to .attributes.yml
    from os.path import dirname, basename, join
    attributes_path = join(dirname(path), ".attributes.yml")

    # retrieve the .attributes.yml structure
    from course.content import get_raw_yaml_from_repo
    try:
        attributes = get_raw_yaml_from_repo(repo, attributes_path,
                                            commit_sha)
    except ObjectDoesNotExist:
        # no attributes file: not accessible
        return False

    path_basename = basename(path)

    # [unenrolled, student, ta, instructor]
    # access_kind hierarchy and who should be allowed in sets:
    # in_exam             : in_exam
    # instructor          : [public, unenrolled, student, ta, instructor]
    # ta                  : [public, unenrolled, student, ta]
    # student             : [public, unenrolled, student]
    # unenrolled          : [public, unenrolled]

    # "public" is a deprecated alias for "unenrolled".

    if access_kind == 'in_exam':
        kind_list = ['in_exam']
    elif access_kind == ('unenrolled' or 'public'):
        kind_list = ['public', 'unenrolled']
    elif access_kind == 'student':
        kind_list = ['public', 'unenrolled', 'student']
    elif access_kind == 'ta':
        kind_list = ['public', 'unenrolled', 'student', 'ta']
    elif access_kind == 'instructor':
        kind_list = ['public', 'unenrolled', 'student', 'ta', 'instructor']

    access_patterns = []
    for kind in kind_list:
        access_patterns += attributes.get(kind, [])

    from fnmatch import fnmatch
    if isinstance(access_patterns, list):
        for pattern in access_patterns:
            if isinstance(pattern, six.string_types):
                if fnmatch(path_basename, pattern):
                    return True

    return False

# }}}


# {{{ jinja interaction

JINJA_YAML_RE = re.compile(
    r"^\[JINJA\]\s*$(.*?)^\[\/JINJA\]\s*$",
    re.MULTILINE | re.DOTALL)
YAML_BLOCK_START_SCALAR_RE = re.compile(
    r"(:\s*[|>])"
    "(J?)"
    "((?:[0-9][-+]?|[-+][0-9]?)?)"
    "(?:\s*\#.*)?"
    "$")

GROUP_COMMENT_START = re.compile(r"^\s*#\s*\{\{\{")
LEADING_SPACES_RE = re.compile(r"^( *)")


def process_yaml_for_expansion(yaml_str):
    lines = yaml_str.split("\n")
    jinja_lines = []

    i = 0
    line_count = len(lines)

    while i < line_count:
        l = lines[i]
        yaml_block_scalar_match = YAML_BLOCK_START_SCALAR_RE.search(l)

        if yaml_block_scalar_match is not None:
            unprocessed_block_lines = []
            allow_jinja = bool(yaml_block_scalar_match.group(2))
            l = YAML_BLOCK_START_SCALAR_RE.sub(
                    r"\1\3", l)

            unprocessed_block_lines.append(l)

            block_start_indent = len(LEADING_SPACES_RE.match(l).group(1))

            i += 1

            while i < line_count:
                l = lines[i]

                if not l.rstrip():
                    unprocessed_block_lines.append(l)
                    i += 1
                    continue

                line_indent = len(LEADING_SPACES_RE.match(l).group(1))
                if line_indent <= block_start_indent:
                    break
                else:
                    unprocessed_block_lines.append(l)
                    i += 1

            if not allow_jinja:
                jinja_lines.append("{% raw %}")
            jinja_lines.extend(unprocessed_block_lines)
            if not allow_jinja:
                jinja_lines.append("{% endraw %}")

        elif GROUP_COMMENT_START.match(l):
            jinja_lines.append("{% raw %}")
            jinja_lines.append(l)
            jinja_lines.append("{% endraw %}")
            i += 1

        else:
            jinja_lines.append(l)
            i += 1

    return "\n".join(jinja_lines)


class GitTemplateLoader(BaseTemplateLoader):
    def __init__(self, repo, commit_sha):
        self.repo = repo
        self.commit_sha = commit_sha

    def get_source(self, environment, template):
        try:
            data = get_repo_blob_data_cached(self.repo, template, self.commit_sha)
        except ObjectDoesNotExist:
            raise TemplateNotFound(template)

        source = data.decode('utf-8')

        def is_up_to_date():
            # There's not much point to caching here, because we create
            # a new loader for every request anyhow...
            return False

        return source, None, is_up_to_date


class YamlBlockEscapingGitTemplateLoader(GitTemplateLoader):
    # https://github.com/inducer/relate/issues/130

    def get_source(self, environment, template):
        source, path, is_up_to_date = \
                super(YamlBlockEscapingGitTemplateLoader, self).get_source(
                        environment, template)

        from os.path import splitext
        _, ext = splitext(template)
        ext = ext.lower()

        if ext in [".yml", ".yaml"]:
            source = process_yaml_for_expansion(source)

        return source, path, is_up_to_date


class YamlBlockEscapingFileSystemLoader(FileSystemLoader):
    # https://github.com/inducer/relate/issues/130

    def get_source(self, environment, template):
        source, path, is_up_to_date = \
                super(YamlBlockEscapingFileSystemLoader, self).get_source(
                        environment, template)

        from os.path import splitext
        _, ext = splitext(template)
        ext = ext.lower()

        if ext in [".yml", ".yaml"]:
            source = process_yaml_for_expansion(source)

        return source, path, is_up_to_date


def expand_yaml_macros(repo, commit_sha, yaml_str):
    if isinstance(yaml_str, six.binary_type):
        yaml_str = yaml_str.decode("utf-8")

    from jinja2 import Environment, StrictUndefined
    jinja_env = Environment(
            loader=YamlBlockEscapingGitTemplateLoader(repo, commit_sha),
            undefined=StrictUndefined)

    # {{{ process explicit [JINJA] tags (deprecated)

    def compute_replacement(match):
        template = jinja_env.from_string(match.group(1))
        return template.render()

    yaml_str, count = JINJA_YAML_RE.subn(compute_replacement, yaml_str)

    if count:
        # The file uses explicit [JINJA] tags. Assume that it doesn't
        # want anything else processed through YAML.
        return yaml_str

    # }}}

    jinja_str = process_yaml_for_expansion(yaml_str)
    template = jinja_env.from_string(jinja_str)
    yaml_str = template.render()

    return yaml_str

# }}}


# {{{ repo yaml getting

def get_raw_yaml_from_repo(repo, full_name, commit_sha):
    """Return decoded YAML data structure from
    the given file in *repo* at *commit_sha*.

    :arg commit_sha: A byte string containing the commit hash
    """

    from six.moves.urllib.parse import quote_plus
    cache_key = "%RAW%%2".join((
        CACHE_KEY_ROOT,
        quote_plus(repo.controldir()), quote_plus(full_name), commit_sha.decode(),
        ))

    import django.core.cache as cache
    def_cache = cache.caches["default"]
    result = None
    # Memcache is apparently limited to 250 characters.
    if len(cache_key) < 240:
        result = def_cache.get(cache_key)
    if result is not None:
        return result

    result = load_yaml(
            expand_yaml_macros(
                repo, commit_sha,
                get_repo_blob(repo, full_name, commit_sha,
                    allow_tree=False).data))

    def_cache.add(cache_key, result, None)

    return result


def get_yaml_from_repo(repo, full_name, commit_sha, cached=True):
    """Return decoded, struct-ified YAML data structure from
    the given file in *repo* at *commit_sha*.

    See :class:`relate.utils.Struct` for more on
    struct-ification.
    """

    if cached:
        from six.moves.urllib.parse import quote_plus
        cache_key = "%%%2".join(
                (CACHE_KEY_ROOT,
                    quote_plus(repo.controldir()), quote_plus(full_name),
                    commit_sha.decode()))

        import django.core.cache as cache
        def_cache = cache.caches["default"]
        result = None
        # Memcache is apparently limited to 250 characters.
        if len(cache_key) < 240:
            result = def_cache.get(cache_key)
        if result is not None:
            return result

    expanded = expand_yaml_macros(
            repo, commit_sha,
            get_repo_blob(repo, full_name, commit_sha,
                allow_tree=False).data)

    result = dict_to_struct(load_yaml(expanded))

    if cached:
        def_cache.add(cache_key, result, None)

    return result


# }}}


# {{{ markup

def _attr_to_string(key, val):
    if val is None:
        return key
    elif "\"" in val:
        return "%s='%s'" % (key, val)
    else:
        return "%s=\"%s\"" % (key, val)


class TagProcessingHTMLParser(html_parser.HTMLParser):
    def __init__(self, out_file, process_tag_func):
        html_parser.HTMLParser.__init__(self)

        self.out_file = out_file
        self.process_tag_func = process_tag_func

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        attrs.update(self.process_tag_func(tag, attrs))

        self.out_file.write("<%s %s>" % (tag, " ".join(
            _attr_to_string(k, v) for k, v in six.iteritems(attrs))))

    def handle_endtag(self, tag):
        self.out_file.write("</%s>" % tag)

    def handle_startendtag(self, tag, attrs):
        attrs = dict(attrs)
        attrs.update(self.process_tag_func(tag, attrs))

        self.out_file.write("<%s %s/>" % (tag, " ".join(
            _attr_to_string(k, v) for k, v in six.iteritems(attrs))))

    def handle_data(self, data):
        self.out_file.write(data)

    def handle_entityref(self, name):
        self.out_file.write("&%s;" % name)

    def handle_charref(self, name):
        self.out_file.write("&#%s;" % name)

    def handle_comment(self, data):
        self.out_file.write("<!--%s-->" % data)

    def handle_decl(self, decl):
        self.out_file.write("<!%s>" % decl)

    def handle_pi(self, data):
        raise NotImplementedError(
                _("I have no idea what a processing instruction is."))

    def unknown_decl(self, data):
        self.out_file.write("<![%s]>" % data)


class PreserveFragment(object):
    def __init__(self, s):
        self.s = s


class LinkFixerTreeprocessor(Treeprocessor):
    def __init__(self, md, course, commit_sha, reverse_func):
        Treeprocessor.__init__(self)
        self.md = md
        self.course = course
        self.commit_sha = commit_sha
        self.reverse_func = reverse_func

    def reverse(self, viewname, args):
        frag = None

        new_args = []
        for arg in args:
            if isinstance(arg, PreserveFragment):
                s = arg.s
                frag_index = s.find("#")
                if frag_index != -1:
                    frag = s[frag_index:]
                    s = s[:frag_index]

                new_args.append(s)
            else:
                new_args.append(arg)

        result = self.reverse_func(viewname, args=new_args)

        if frag is not None:
            result += frag

        return result

    def get_course_identifier(self):
        if self.course is None:
            return "bogus-course-identifier"
        else:
            return self.course.identifier

    def process_url(self, url):
        try:
            if url.startswith("course:"):
                course_id = url[7:]
                if course_id:
                    return self.reverse("relate-course_page",
                                args=(course_id,))
                else:
                    return self.reverse("relate-course_page",
                                args=(self.get_course_identifier(),))

            elif url.startswith("flow:"):
                flow_id = url[5:]
                return self.reverse("relate-view_start_flow",
                            args=(self.get_course_identifier(), flow_id))

            elif url.startswith("staticpage:"):
                page_path = url[11:]
                return self.reverse("relate-content_page",
                            args=(
                                self.get_course_identifier(),
                                PreserveFragment(page_path)))

            elif url.startswith("media:"):
                media_path = url[6:]
                return self.reverse("relate-get_media",
                            args=(
                                self.get_course_identifier(),
                                self.commit_sha,
                                PreserveFragment(media_path)))

            elif url.startswith("repo:"):
                path = url[5:]
                return self.reverse("relate-get_repo_file",
                            args=(
                                self.get_course_identifier(),
                                self.commit_sha,
                                PreserveFragment(path)))

            elif url.startswith("repocur:"):
                path = url[8:]
                return self.reverse("relate-get_current_repo_file",
                            args=(
                                self.get_course_identifier(),
                                PreserveFragment(path)))

            elif url.strip() == "calendar:":
                return self.reverse("relate-view_calendar",
                            args=(self.get_course_identifier(),))

        except NoReverseMatch:
            from base64 import b64encode
            message = ("Invalid character in RELATE URL: " + url).encode("utf-8")
            return "data:text/plain;base64,"+b64encode(message).decode()

        return None

    def process_tag(self, tag_name, attrs):
        changed_attrs = {}

        if tag_name == "table":
            changed_attrs["class"] = "table table-condensed"

        if tag_name in ["a", "link"] and "href" in attrs:
            new_href = self.process_url(attrs["href"])

            if new_href is not None:
                changed_attrs["href"] = new_href

        elif tag_name == "img" and "src" in attrs:
            new_src = self.process_url(attrs["src"])

            if new_src is not None:
                changed_attrs["src"] = new_src

        elif tag_name == "object" and "data" in attrs:
            new_data = self.process_url(attrs["data"])

            if new_data is not None:
                changed_attrs["data"] = new_data

        return changed_attrs

    def process_etree_element(self, element):
        changed_attrs = self.process_tag(element.tag, element.attrib)

        for key, val in six.iteritems(changed_attrs):
            element.set(key, val)

    def walk_and_process_tree(self, root):
        self.process_etree_element(root)

        for child in root:
            self.walk_and_process_tree(child)

    def run(self, root):
        self.walk_and_process_tree(root)

        # root through and process Markdown's HTML stash (gross!)
        from six.moves import cStringIO

        for i, (html, safe) in enumerate(self.md.htmlStash.rawHtmlBlocks):
            outf = cStringIO()
            parser = TagProcessingHTMLParser(outf, self.process_tag)
            parser.feed(html)

            self.md.htmlStash.rawHtmlBlocks[i] = (outf.getvalue(), safe)


class LinkFixerExtension(Extension):
    def __init__(self, course, commit_sha, reverse_func):
        Extension.__init__(self)
        self.course = course
        self.commit_sha = commit_sha
        self.reverse_func = reverse_func

    def extendMarkdown(self, md, md_globals):  # noqa
        md.treeprocessors["relate_link_fixer"] = \
                LinkFixerTreeprocessor(md, self.course, self.commit_sha,
                        reverse_func=self.reverse_func)


def remove_prefix(prefix, s):
    if s.startswith(prefix):
        return s[len(prefix):]
    else:
        return s


JINJA_PREFIX = "[JINJA]"


def markup_to_html(course, repo, commit_sha, text, reverse_func=None,
        validate_only=False, jinja_env={}):
    if reverse_func is None:
        from django.core.urlresolvers import reverse
        reverse_func = reverse

    if course is not None and not jinja_env:
        try:
            import django.core.cache as cache
        except ImproperlyConfigured:
            cache_key = None
        else:
            import hashlib
            cache_key = ("markup:v6:%s:%d:%s:%s"
                    % (CACHE_KEY_ROOT, course.id, str(commit_sha),
                        hashlib.md5(text.encode("utf-8")).hexdigest()))

            def_cache = cache.caches["default"]
            result = def_cache.get(cache_key)
            if result is not None:
                assert isinstance(result, six.text_type)
                return result

        if text.lstrip().startswith(JINJA_PREFIX):
            text = remove_prefix(JINJA_PREFIX, text.lstrip())
    else:
        cache_key = None

    if not isinstance(text, six.text_type):
        text = six.text_type(text)

    # {{{ process through Jinja

    from jinja2 import Environment, StrictUndefined
    env = Environment(
            loader=GitTemplateLoader(repo, commit_sha),
            undefined=StrictUndefined)
    template = env.from_string(text)
    text = template.render(**jinja_env)

    # }}}

    if validate_only:
        return

    from course.mdx_mathjax import MathJaxExtension
    import markdown
    result = markdown.markdown(text,
        extensions=[
            LinkFixerExtension(course, commit_sha, reverse_func=reverse_func),
            MathJaxExtension(),
            "markdown.extensions.extra",
            "markdown.extensions.codehilite",
            ],
        output_format="html5")

    assert isinstance(result, six.text_type)
    if cache_key is not None:
        def_cache.add(cache_key, result, None)

    return result


TITLE_RE = re.compile(r"^\#+\s*(\w.*)", re.UNICODE)


def extract_title_from_markup(markup_text):
    lines = markup_text.split("\n")

    for l in lines[:10]:
        match = TITLE_RE.match(l)
        if match is not None:
            return match.group(1)

    return None

# }}}


# {{{ datespec processing

DATE_RE = re.compile(r"^([0-9]+)\-([01][0-9])\-([0-3][0-9])$")
TRAILING_NUMERAL_RE = re.compile(r"^(.*)\s+([0-9]+)$")

END_PREFIX = "end:"


class InvalidDatespec(ValueError):
    def __init__(self, datespec):
        ValueError.__init__(self, str(datespec))
        self.datespec = datespec


AT_TIME_RE = re.compile(r"^(.*)\s*@\s*([0-2]?[0-9])\:([0-9][0-9])\s*$")


class AtTimePostprocessor(object):
    def __init__(self, hour, minute, second=0):
        self.hour = hour
        self.minute = minute
        self.second = second

    @classmethod
    def parse(cls, s):
        match = AT_TIME_RE.match(s)
        if match is not None:
            hour = int(match.group(2))
            minute = int(match.group(3))

            if not (0 <= hour < 24):
                raise InvalidDatespec(s)

            if not (0 <= minute < 60):
                raise InvalidDatespec(s)

            return match.group(1), AtTimePostprocessor(hour, minute)
        else:
            return s, None

    def apply(self, dtm):
        from pytz import timezone
        server_tz = timezone(settings.TIME_ZONE)

        return dtm.astimezone(server_tz).replace(
                    hour=self.hour,
                    minute=self.minute,
                    second=self.second)


PLUS_DELTA_RE = re.compile(r"^(.*)\s*([+-])\s*([0-9]+)\s+"
    "(weeks?|days?|hours?|minutes?)$")


class PlusDeltaPostprocessor(object):
    def __init__(self, count, period):
        self.count = count
        self.period = period

    @classmethod
    def parse(cls, s):
        match = PLUS_DELTA_RE.match(s)
        if match is not None:
            count = int(match.group(3))
            if match.group(2) == "-":
                count = -count
            period = match.group(4)

            return match.group(1), PlusDeltaPostprocessor(count, period)
        else:
            return s, None

    def apply(self, dtm):
        if self.period.startswith("week"):
            d = datetime.timedelta(weeks=self.count)
        elif self.period.startswith("day"):
            d = datetime.timedelta(days=self.count)
        elif self.period.startswith("hour"):
            d = datetime.timedelta(hours=self.count)
        elif self.period.startswith("minute"):
            d = datetime.timedelta(minutes=self.count)
        else:
            raise InvalidDatespec(_("invalid period: %s" % self.period))

        return dtm + d


DATESPEC_POSTPROCESSORS = [
        AtTimePostprocessor,
        PlusDeltaPostprocessor,
        ]


def parse_date_spec(course, datespec, vctx=None, location=None):
    if datespec is None:
        return None

    orig_datespec = datespec

    def localize_if_needed(d):
        if d.tzinfo is None:
            from relate.utils import localize_datetime
            return localize_datetime(d)
        else:
            return d

    if isinstance(datespec, datetime.datetime):
        return localize_if_needed(datespec)
    if isinstance(datespec, datetime.date):
        return localize_if_needed(
                datetime.datetime.combine(datespec, datetime.time.min))

    datespec = datespec.strip()

    # {{{ parse postprocessors

    postprocs = []
    while True:
        parsed_one = False
        for pp_class in DATESPEC_POSTPROCESSORS:
            datespec, postproc = pp_class.parse(datespec)
            if postproc is not None:
                parsed_one = True
                postprocs.insert(0, postproc)
                break

        datespec = datespec.strip()

        if not parsed_one:
            break

    # }}}

    def apply_postprocs(dtime):
        for postproc in postprocs:
            dtime = postproc.apply(dtime)

        return dtime

    match = DATE_RE.match(datespec)
    if match:
        result = datetime.date(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)))
        result = localize_if_needed(
                datetime.datetime.combine(result, datetime.time.min))
        return apply_postprocs(result)

    is_end = datespec.startswith(END_PREFIX)
    if is_end:
        datespec = datespec[len(END_PREFIX):]

    match = TRAILING_NUMERAL_RE.match(datespec)
    if match:
        # event with numeral

        event_kind = match.group(1)
        ordinal = int(match.group(2))

    else:
        # event without numeral

        event_kind = datespec
        ordinal = None

    if vctx is not None:
        from course.validation import validate_identifier
        validate_identifier(vctx, "%s: event kind" % location, event_kind)

    if course is None:
        return now()

    from course.models import Event

    try:
        event_obj = Event.objects.get(
            course=course,
            kind=event_kind,
            ordinal=ordinal)

    except ObjectDoesNotExist:
        if vctx is not None:
            vctx.add_warning(
                    location,
                    _("unrecognized date/time specification: '%s' "
                    "(interpreted as 'now')")
                    % orig_datespec)
        return now()

    if is_end:
        if event_obj.end_time is not None:
            result = event_obj.end_time
        else:
            result = event_obj.time
            if vctx is not None:
                vctx.add_warning(
                        location,
                        _("event '%s' has no end time, using start time instead")
                        % orig_datespec)

    else:
        result = event_obj.time

    return apply_postprocs(result)


# }}}


# {{{ page chunks

def compute_chunk_weight_and_shown(course, chunk, role, now_datetime,
        facilities):
    if not hasattr(chunk, "rules"):
        return 0, True

    for rule in chunk.rules:
        if hasattr(rule, "if_has_role"):
            if role not in rule.if_has_role:
                continue

        if hasattr(rule, "if_after"):
            start_date = parse_date_spec(course, rule.if_after)
            if now_datetime < start_date:
                continue

        if hasattr(rule, "if_before"):
            end_date = parse_date_spec(course, rule.if_before)
            if end_date < now_datetime:
                continue

        if hasattr(rule, "if_in_facility"):
            if rule.if_in_facility not in facilities:
                continue

        # {{{ deprecated

        if hasattr(rule, "roles"):
            if role not in rule.roles:
                continue

        if hasattr(rule, "start"):
            start_date = parse_date_spec(course, rule.start)
            if now_datetime < start_date:
                continue

        if hasattr(rule, "end"):
            end_date = parse_date_spec(course, rule.end)
            if end_date < now_datetime:
                continue

        # }}}

        shown = True
        if hasattr(rule, "shown"):
            shown = rule.shown

        return rule.weight, shown

    return 0, True


def get_processed_page_chunks(course, repo, commit_sha,
        page_desc, role, now_datetime, facilities):
    for chunk in page_desc.chunks:
        chunk.weight, chunk.shown = \
                compute_chunk_weight_and_shown(
                        course, chunk, role, now_datetime,
                        facilities)
        chunk.html_content = markup_to_html(course, repo, commit_sha, chunk.content)
        if not hasattr(chunk, "title"):
            from course.content import extract_title_from_markup
            chunk.title = extract_title_from_markup(chunk.content)

    page_desc.chunks.sort(key=lambda chunk: chunk.weight, reverse=True)

    return [chunk for chunk in page_desc.chunks
            if chunk.shown]


# }}}


# {{{ repo desc getting

def normalize_page_desc(page_desc):
    if hasattr(page_desc, "content"):
        content = page_desc.content
        from relate.utils import struct_to_dict, Struct
        d = struct_to_dict(page_desc)
        del d["content"]
        d["chunks"] = [Struct({"id": "main", "content": content})]
        return Struct(d)

    return page_desc


def get_staticpage_desc(repo, course, commit_sha, filename):
    page_desc = get_yaml_from_repo(repo, filename, commit_sha)
    page_desc = normalize_page_desc(page_desc)
    return page_desc


def get_course_desc(repo, course, commit_sha):
    return get_staticpage_desc(repo, course, commit_sha, course.course_file)


def normalize_flow_desc(flow_desc):
    if hasattr(flow_desc, "pages"):
        pages = flow_desc.pages
        from relate.utils import struct_to_dict, Struct
        d = struct_to_dict(flow_desc)
        del d["pages"]
        d["groups"] = [Struct({"id": "main", "pages": pages})]
        return Struct(d)

    if hasattr(flow_desc, "rules"):
        rules = flow_desc.rules
        if not hasattr(rules, "grade_identifier"):
            # Legacy content with grade_identifier in grading rule,
            # move first found grade_identifier up to rules.

            rules.grade_identifier = None
            rules.grade_aggregation_strategy = None

            for grule in rules.grading:
                if grule.grade_identifier is not None:
                    rules.grade_identifier = grule.grade_identifier
                    rules.grade_aggregation_strategy = \
                            grule.grade_aggregation_strategy
                    break

    return flow_desc


def get_flow_desc(repo, course, flow_id, commit_sha):
    flow_desc = get_yaml_from_repo(repo, "flows/%s.yml" % flow_id, commit_sha)

    flow_desc = normalize_flow_desc(flow_desc)

    flow_desc.description_html = markup_to_html(
            course, repo, commit_sha, getattr(flow_desc, "description", None))
    return flow_desc


def get_flow_page_desc(flow_id, flow_desc, group_id, page_id):
    for grp in flow_desc.groups:
        if grp.id == group_id:
            for page in grp.pages:
                if page.id == page_id:
                    return page

    raise ObjectDoesNotExist(
            _("page '%(group_id)s/%(page_id)s' in flow '%(flow_id)s'") % {
                'group_id': group_id,
                'page_id': page_id,
                'flow_id': flow_id
                })

# }}}


# {{{ flow page handling

class ClassNotFoundError(RuntimeError):
    pass


def import_class(name):
    components = name.split('.')

    if len(components) < 2:
        # need at least one module plus class name
        raise ClassNotFoundError(name)

    module_name = ".".join(components[:-1])
    try:
        mod = __import__(module_name)
    except ImportError:
        raise ClassNotFoundError(name)

    for comp in components[1:]:
        try:
            mod = getattr(mod, comp)
        except AttributeError:
            raise ClassNotFoundError(name)

    return mod


def get_flow_page_class(repo, typename, commit_sha):
    # look among default page types
    import course.page
    try:
        return getattr(course.page, typename)
    except AttributeError:
        pass

    # try a global dotted-name import
    try:
        return import_class(typename)
    except ClassNotFoundError:
        pass

    if typename.startswith("repo:"):
        stripped_typename = typename[5:]

        components = stripped_typename.split(".")
        if len(components) != 2:
            raise ClassNotFoundError(
                    _("repo page class must conist of two "
                    "dotted components (invalid: '%s')")
                    % typename)

        module, classname = components
        module_name = "code/"+module+".py"
        module_code = get_repo_blob(repo, module_name, commit_sha,
                allow_tree=False).data

        module_dict = {}

        exec(compile(module_code, module_name, 'exec'), module_dict)

        try:
            return module_dict[classname]
        except AttributeError:
            raise ClassNotFoundError(typename)
    else:
        raise ClassNotFoundError(typename)


def instantiate_flow_page(location, repo, page_desc, commit_sha):
    class_ = get_flow_page_class(repo, page_desc.type, commit_sha)

    return class_(None, location, page_desc)

# }}}


def get_course_commit_sha(course, participation):
    # logic duplicated in course.utils.CoursePageContext

    sha = course.active_git_commit_sha

    if participation is not None and participation.preview_git_commit_sha:
        preview_sha = participation.preview_git_commit_sha

        repo = get_course_repo(course)
        if isinstance(repo, SubdirRepoWrapper):
            repo = repo.repo

        try:
            repo[preview_sha.encode()]
        except KeyError:
            preview_sha = None

        if preview_sha is not None:
            sha = preview_sha

    return sha.encode()


def list_flow_ids(repo, commit_sha):
    flow_ids = []
    try:
        flows_tree = get_repo_blob(repo, "flows", commit_sha)
    except ObjectDoesNotExist:
        # That's OK--no flows yet.
        pass
    else:
        for entry in flows_tree.items():
            if entry.path.endswith(b".yml"):
                flow_ids.append(entry.path[:-4])

    return sorted(flow_ids)

# vim: foldmethod=marker
