"""Bounded inspection of public documents and plugin archives. Never executes code."""

import hashlib
import http.client
import io
import ipaddress
import json
from pathlib import PurePosixPath
import re
import socket
import ssl
import stat
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile


class InvalidArtifact(ValueError):
    pass


class UnsupportedCompatibility(ValueError):
    pass


class CompatibilityMismatch(InvalidArtifact):
    def __init__(self, message, manifest, content):
        super().__init__(message)
        self.manifest = manifest
        self.digest = hashlib.sha256(content).hexdigest()


class UnsafeURL(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once, reject every private/reserved address, and connect to the
        # validated address itself. TLS still verifies the original hostname.
        deadline = time.monotonic() + (self.timeout if isinstance(self.timeout, (int, float)) else 15)
        addresses = socket.getaddrinfo(self.host, self.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise UnsafeURL("URL resolves to a non-public address")
        last = OSError("Public HTTPS connection timed out")
        addresses.sort(key=lambda row: row[0] != socket.AF_INET)
        for family, kind, proto, _, endpoint in addresses[:4]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection = socket.socket(family, kind, proto)
            connection.settimeout(min(5, remaining))
            try:
                connection.connect(endpoint)
                self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
                return
            except OSError as exc:
                connection.close()
                last = exc
        raise last


class PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PublicHTTPSConnection, request, context=ssl.create_default_context())


def public_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in {None, 443}):
            raise UnsafeURL("Expected a public HTTPS URL on port 443")
        hostname = parsed.hostname.lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            raise UnsafeURL("Local documentation hosts are not public evidence")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise UnsafeURL("Non-public IP addresses are not public evidence")
    except ValueError as exc:
        raise UnsafeURL("Malformed public HTTPS URL") from exc
    return parsed


def public_fetch(url, limit, before_request=lambda: None, allowed_hosts=None, probe=False):
    """Pin public DNS on every hop; never attach credentials or use environment proxies."""
    opener = build_opener(ProxyHandler({}), NoRedirect(), PublicHTTPSHandler())
    for _ in range(5):
        parsed = public_url(url)
        if allowed_hosts is not None and parsed.hostname.lower() not in allowed_hosts:
            raise UnsafeURL("Download redirected outside GitHub's asset hosts")
        before_request()
        request = Request(url, headers={"User-Agent": "awesome-zotero-curation"})
        try:
            response = opener.open(request, timeout=15)
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308} and exc.headers.get("Location"):
                url = urljoin(url, exc.headers["Location"])
                exc.close()
                continue
            raise
        with response:
            if probe:
                return b""
            if response.headers.get("Content-Length", "").isdigit() and int(response.headers["Content-Length"]) > limit:
                raise UnsafeURL("Public response exceeds inspection size limit")
            content = response.read(limit + 1)
            if len(content) > limit:
                raise UnsafeURL("Public response exceeds inspection size limit")
            return content
    raise UnsafeURL("Too many redirects")


def numeric_version(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise InvalidArtifact("Missing or excessively long Zotero version bound")
    if not re.fullmatch(r"\d+(?:\.\d+){0,3}", value):
        raise UnsupportedCompatibility("Non-numeric Mozilla version bounds require a separate compatibility check")
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (4 - len(parts))


def compatible(version, minimum, maximum):
    current = numeric_version(version)
    lower = numeric_version(minimum)
    if not isinstance(maximum, str):
        raise InvalidArtifact("Plugin must declare strict_max_version")
    if maximum == "*":
        upper = (float("inf"),) * 4
    elif maximum.endswith(".*"):
        prefix = maximum[:-2]
        parsed = numeric_version(prefix)
        count = len(prefix.split("."))
        upper = parsed[:count] + (float("inf"),) * (4 - count)
    else:
        upper = numeric_version(maximum)
    return lower <= current <= upper


def inspect_xpi(content, tested_zotero, current_zotero, max_bytes, overrides=None):
    if len(content) > max_bytes:
        raise InvalidArtifact("Plugin archive exceeds the configured download limit")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            normalized = [PurePosixPath(name).as_posix().rstrip("/") for name in names]
            if len(members) > 2000 or len(set(normalized)) != len(names):
                raise InvalidArtifact("Plugin archive has excessive or duplicate entries")
            if sum(member.file_size for member in members) > 100 * 1024 * 1024:
                raise InvalidArtifact("Plugin archive exceeds the expanded size limit")
            for member in members:
                path = PurePosixPath(member.filename)
                if (path.is_absolute() or ".." in path.parts or "\\" in member.filename
                        or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1):
                    raise InvalidArtifact("Plugin archive contains unsafe or encrypted entries")
            for name in ("manifest.json", "bootstrap.js"):
                if name not in names or not archive.getinfo(name).file_size:
                    raise InvalidArtifact(f"Plugin archive lacks a nonempty root {name}")
            if archive.getinfo("manifest.json").file_size > 128 * 1024:
                raise InvalidArtifact("Plugin manifest exceeds the inspection limit")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("manifest_version") != 2:
                raise InvalidArtifact("Plugin must contain a Zotero manifest_version 2 manifest")
            settings = manifest.get("applications", {}).get("zotero", {})
            if not isinstance(settings, dict) or not isinstance(settings.get("id"), str) or not settings["id"].strip():
                raise InvalidArtifact("Plugin manifest lacks applications.zotero.id")
            for field in ("name", "version"):
                if not isinstance(manifest.get(field), str) or not manifest[field].strip():
                    raise InvalidArtifact(f"Plugin manifest lacks {field}")
            effective = dict(settings, **(overrides or {}))
            minimum, maximum = effective.get("strict_min_version"), effective.get("strict_max_version")
            for version in {tested_zotero, current_zotero}:
                if not compatible(version, minimum, maximum):
                    raise CompatibilityMismatch(f"Plugin manifest does not declare compatibility with Zotero {version}", manifest, content)
            return {"id": settings["id"], "version": manifest["version"],
                    "minimum": minimum, "maximum": maximum,
                    "sha256": hashlib.sha256(content).hexdigest()}
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeError, KeyError, TypeError, AttributeError,
            NotImplementedError, RuntimeError) as exc:
        raise InvalidArtifact("Plugin archive or manifest is invalid") from exc
