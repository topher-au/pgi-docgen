# Copyright 2013,2014 Christoph Reiter
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

import gc
import re
from xml.dom import minidom

from . import util


def get_namespace(namespace, version, _ns_cache={}):
    key = namespace, version
    if not key in _ns_cache:
        ns = Namespace(namespace, version)
        _ns_cache[key] = ns
    return _ns_cache[key]


def _get_dom(path, _cache={}):
    # caches the last dom
    if path in _cache:
        return _cache[path]
    _cache.clear()
    # reduce peak memory
    gc.collect()
    _cache[path] = minidom.parse(path)
    return _cache[path]


class Namespace(object):

    def __init__(self, namespace, version):
        self.namespace = namespace
        self.version = version

        print "Parsing GIR (%s-%s)" % (namespace, version)
        dom = _get_dom(self.path)

        self._types = _parse_types(dom, namespace)

        # dependencies
        deps = []
        for include in dom.getElementsByTagName("include"):
            name = include.getAttribute("name")
            version = include.getAttribute("version")
            deps.append((name, version))
        self._dependencies = deps

    def parse_private(self):
        return _parse_private(_get_dom(self.path), self.namespace)

    def parse_docs(self):
         return _parse_docs(_get_dom(self.path))

    def get_types(self):
        return self._types

    @property
    def path(self):
        key = "%s-%s" % (self.namespace, self.version)
        return util.get_gir_files()[key]

    def get_dependencies(self):
        return list(self._dependencies)


def _parse_types(dom, namespace):
    """Create a mapping of various C names to python names"""

    types = {}

    def add(c_name, py_name):
        if c_name in types:
            old_count = types[c_name].count(".")
            new_count = py_name.count(".")
            # prefer static methods over functions
            if old_count > new_count:
                return

        assert py_name.count("."), py_name

        # escape each potential attribute
        py_name = ".".join(
            map(util.escape_identifier,  py_name.split(".")))
        types[c_name] = py_name

    # {key of the to be replaces function: c def of the replacement}
    shadowed = {}

    # gtk_main -> Gtk.main
    # gtk_dialog_get_response_for_widget ->
    #     Gtk.Dialog.get_response_for_widget
    elements = dom.getElementsByTagName("function")
    elements += dom.getElementsByTagName("constructor")
    elements += dom.getElementsByTagName("method")
    for t in elements:
        shadows = t.getAttribute("shadows")
        local_name = t.getAttribute("name")
        c_name = t.getAttribute("c:identifier")
        assert c_name

        # Copy escaping from gi: Foo.break -> Foo.break_
        full_name = local_name
        parent = t.parentNode

        # glib:boxed toplevel in Farstream-0.1
        if not parent.getAttribute("name"):
            continue

        while parent.getAttribute("name"):
            full_name = parent.getAttribute("name") + "." + full_name
            parent = parent.parentNode

        if shadows:
            shadowed_name = ".".join(full_name.split(".")[:-1] + [shadows])
            shadowed_name = ".".join(
                map(util.escape_identifier, shadowed_name.split(".")))
            shadowed[shadowed_name] = c_name

        add(c_name, full_name)

    # enums etc. GTK_SOME_FLAG_FOO -> Gtk.SomeFlag.FOO
    for t in dom.getElementsByTagName("member"):
        c_name = t.getAttribute("c:identifier")
        assert c_name
        class_name = t.parentNode.getAttribute("name")
        field_name = t.getAttribute("name").upper()
        local_name = namespace + "." + class_name + "." + field_name
        add(c_name, local_name)

    # classes
    elements = dom.getElementsByTagName("class")
    elements += dom.getElementsByTagName("interface")
    elements += dom.getElementsByTagName("enumeration")
    elements += dom.getElementsByTagName("bitfield")
    elements += dom.getElementsByTagName("callback")
    elements += dom.getElementsByTagName("union")
    elements += dom.getElementsByTagName("alias")
    for t in elements:
        # only top level
        if t.parentNode.tagName != "namespace":
            continue

        c_name = t.getAttribute("c:type")
        c_name = c_name or t.getAttribute("glib:type-name")

        # e.g. GObject _Value__data__union
        if not c_name:
            continue

        type_name = t.getAttribute("name")
        add(c_name, namespace + "." + type_name)

    # cairo_t -> cairo.Context
    for t in dom.getElementsByTagName("record"):
        c_name = t.getAttribute("c:type")
        # Gee-0.8 HazardPointer
        if not c_name:
            continue
        type_name = t.getAttribute("name")
        add(c_name, namespace + "." + type_name)

    # G_TIME_SPAN_MINUTE -> GLib.TIME_SPAN_MINUTE
    for t in dom.getElementsByTagName("constant"):
        c_name = t.getAttribute("c:type")
        if t.parentNode.tagName == "namespace":
            name = namespace + "." + t.getAttribute("name")
            add(c_name, name)

    # the keys we want to replace have should exist
    assert not (set(shadowed.keys()) - set(types.values()))

    # make c defs which are replaced point to the key of the replacement
    # so that: "gdk_threads_add_timeout_full" -> Gdk.threads_add_timeout
    for c_name, name in types.items():
        if name in shadowed:
            replacement = shadowed[name]
            types[replacement] = name

    if namespace == "GObject":
        # these come from overrides and aren't in the gir
        # e.g. G_TYPE_INT -> GObject.TYPE_INT
        from gi.repository import GObject

        for key in dir(GObject):
            if key.startswith("TYPE_"):
                types["G_" + key] = "GObject." + key
            elif key.startswith(("G_MAX", "G_MIN")):
                types[key] = "GObject." + key

        types["GBoxed"] = "GObject.GBoxed"
    elif namespace == "GLib":
        from gi.repository import GLib

        for k in dir(GLib):
            if re.match("MINU?INT\d+", k) or re.match("MAXU?INT\d+", k):
                types["G_" + k] = "GLib." + k

    return types


def _parse_private(dom, namespace):
    private = set()

    # if disguised and no record content... not perfect, but
    # we have no other way
    for record in dom.getElementsByTagName("record"):
        disguised = bool(int(record.getAttribute("disguised") or "0"))
        if disguised:
            children = record.childNodes
            if len(children) == 1 and \
                    children[0].nodeType == children[0].TEXT_NODE:
                name = namespace + "." + record.getAttribute("name")
                private.add(name)

    return private


def _parse_docs(dom):
    """Parse docs"""

    all_ = {}
    parameters = {}
    returns = {}
    signals = {}
    properties = {}
    fields = {}

    tag_names = {
        "glib:signal": signals,
        "field": fields,
        "property": properties,
        "parameter": parameters,
        "instance-parameter": parameters,
        "return-value": returns,
        "interface": all_,
        "method": all_,
        "function": all_,
        "constant": all_,
        "record": all_,
        "enumeration": all_,
        "member": all_,
        "callback": all_,
        "alias": all_,
        "constructor": all_,
        "class": all_,
        # "virtual-method": all_, # warning: name clash with methods
        "bitfield": all_,
    }

    def get_child_by_tag(node, tag_name):
        for sub in node.childNodes:
            try:
                if sub.tagName == tag_name:
                    return sub
            except AttributeError:
                continue

    for tag, result in tag_names.items():
        for e in dom.getElementsByTagName(tag):
            doc_elm = get_child_by_tag(e, "doc")
            docs = (doc_elm and doc_elm.firstChild.nodeValue) or ""
            version = e.getAttribute("version")
            deprecated = e.getAttribute("deprecated")
            deprecated_version = e.getAttribute("deprecated-version")

            l = []
            tags = []
            current = e
            l.append(current.getAttribute("name"))
            while current.tagName != "namespace":
                tags.append(current.tagName)
                current = current.parentNode
                # Tracker-0.16 includes <constant> outside of <namespace>
                if current.tagName == "repository":
                    break
                name = current.getAttribute("name")
                l.insert(0, name)

            # avoid name clashes for stuff we don't support yet
            if "virtual-method" in tags:
                continue
            if tag in ("parameter", "return-value") and "glib:signal" in tags:
                continue

            # special case: GLib.IConv._
            if not l[-1]:
                l[-1] = "_"
            l = filter(None, l)
            key = ".".join(map(util.escape_identifier, l))

            # FIXME: still name clashes somewhere.. (e.g. Atspi-2.0)
            #assert key not in result, (key, tags, result[key], tag, docs)
            result[key] = (docs, version, deprecated_version, deprecated)

    return all_, parameters, returns, signals, properties, fields
