#!/usr/bin/env python3
"""Shared rules for the toolkit's local web servers (workbench, spriteedit, trackeredit, explore).

    import localserver
    def do_GET(self):
        if not localserver.guard(self, post=False): return
    def do_POST(self):
        body = localserver.read_json(self)       # None: it has already answered
        if body is None: return
    out = localserver.confine(path, localserver.roots(rom))     # ValueError if outside

None of these servers has a password and all of them can write files, so each must answer
only its own page. A request is refused when

  * its Host is not this machine's own address and the server's port -- a DNS-rebinding page
    reaches 127.0.0.1 under its own name;
  * it carries a foreign Origin -- a form or `fetch(mode:"no-cors")` from another site;
  * a POST is not `application/json` -- the content types a form can send are not;
  * its body is larger than 8 MB or its Content-Length is not a number.

`confine` keeps a file the page asks a server to write inside the folders the user is working
in: the cartridge's own, the current directory, and anything in A7800_EDIT_ROOT (the
workbench sets that to its project folder).
This is a module, not a tool: it has no command line.
"""
import json
import os

MAX_BODY = 8 * 1024 * 1024


def _hosts(handler):
    port = handler.server.server_address[1]
    return {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}


def guard(handler, post):
    """True if the request may proceed; otherwise the answer has been sent."""
    hosts = _hosts(handler)
    if handler.headers.get("Host", "") not in hosts:
        handler._send(403, {"error": "this server answers only on its own address"})
        return False
    origin = handler.headers.get("Origin")
    if origin and origin not in {"http://" + h for h in hosts}:
        handler._send(403, {"error": "cross-site request refused"})
        return False
    if post:
        ctype = handler.headers.get("Content-Type", "").lower().split(";")[0].strip()
        if ctype != "application/json":
            handler._send(415, {"error": "POST bodies are application/json"})
            return False
    return True


def read_json(handler):
    """The request body as a dict, or None after answering 400/413/415."""
    if not guard(handler, True):
        return None
    try:
        n = int(handler.headers.get("Content-Length", 0))
    except ValueError:
        handler._send(400, {"error": "bad Content-Length"})
        return None
    if n < 0 or n > MAX_BODY:
        handler._send(413, {"error": "request too large"})
        return None
    try:
        body = json.loads(handler.rfile.read(n) or b"{}")
    except ValueError:
        handler._send(400, {"error": "bad JSON"})
        return None
    if not isinstance(body, dict):
        handler._send(400, {"error": "the request body is a JSON object"})
        return None
    return body


def roots(*files):
    """Folders a page may write into: those of `files`, the current directory, and the
    A7800_EDIT_ROOT folders."""
    out = [os.getcwd()]
    for f in files:
        if f:
            out.append(os.path.dirname(os.path.abspath(f)))
    out += [p for p in os.environ.get("A7800_EDIT_ROOT", "").split(os.pathsep) if p]
    return out


def confine(path, allowed):
    """`path` as an absolute path, if it lies inside one of `allowed`; else ValueError."""
    if not isinstance(path, (str, os.PathLike)):
        raise ValueError("a path is a string")
    full = os.path.realpath(os.path.abspath(str(path)))
    for r in allowed:
        root = os.path.realpath(r)
        if full == root or full.startswith(root + os.sep):
            return full
    raise ValueError("%s is outside the folders this editor may write to" % path)
