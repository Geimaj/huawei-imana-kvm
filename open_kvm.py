from __future__ import annotations

import argparse
import getpass
import html
import ssl
import subprocess
import tempfile
import threading
import urllib
import urllib.error
import urllib.parse
import urllib.request
import warnings
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

JAVA8_APPLETVIEWER = Path(
    "/Library/Java/JavaVirtualMachines/temurin-8.jdk/Contents/Home/bin/appletviewer"
)


class LegacyTLSAdapter(HTTPAdapter):
    """Allow HTTPS connections to old iBMC firmware."""

    def __init__(self, *args, **kwargs):
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
        self.ssl_context.minimum_version = ssl.TLSVersion.TLSv1
        self.ssl_context.set_ciphers("DEFAULT:@SECLEVEL=0")

        if legacy_server_connect := getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0):
            self.ssl_context.options |= legacy_server_connect

        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(
            connections, maxsize, block=block, **pool_kwargs
        )


class ParamParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.params: dict[str, str] = {}
        self.attributes: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(k).lower(): (v or "") for k, v in attrs}
        self.attributes.append(values)
        if tag.lower() == "param":
            name = values.get("name", "").lower()
            value = values.get("value", "")
            if name:
                self.params[name] = html.unescape(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a Huawei RH2288H V2 iMana Java KVM console."
    )
    parser.add_argument("ip", help="iMana IP address or hostname")
    parser.add_argument("username", help="iMana username")
    parser.add_argument("--password", help="Password; omit to enter securely")
    parser.add_argument("--cookie", help="pass cookie to reuse active session")
    parser.add_argument("--shared", action="store_true", help="Use shared mode")
    parser.add_argument("--keep", action="store_true", help="Keep JAR and JNLP files")
    return parser.parse_args()


def normalise_host(value: str) -> str:
    value = value.strip().rstrip("/")
    if value.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(value)
        if not parsed.netloc:
            raise ValueError(f"Invalid iMana address: {value}")
        return parsed.netloc
    return value


def login(
    session: requests.Session,
    base_url: str,
    username: str,
    password: str,
) -> None:
    session.get(base_url)
    session.get(f"{base_url}/login.asp?lang=en")

    form = {
        "lang": "end",
        "UserName": username,
        "Password": password,
        "authenticateType": "0",
        "domain": "0",
    }

    response = session.post(f"{base_url}/goform/Login", data=form)
    text = response.text

    if "index.asp" not in text:
        print(response.text)
        print("trying again...")
        response = session.post(f"{base_url}/goform/Login", data=form)
        text = response.text

    if "index.asp" not in text:
        raise RuntimeError(
            "iMana login failed. Check the credentials and remote-console permissions."
        )

    print("logged in")


def extract_console_data(page: str) -> tuple[str, str, str, str]:
    parser = ParamParser()
    parser.feed(page)

    verify_value = parser.params.get("verifyvalue")
    type_data = parser.params.get("typedata")
    mm_verify_value = parser.params.get("mmverifyvalue", verify_value or "")
    codebase = parser.params.get("codebase", "")
    archive = parser.params.get("archive", "")

    for attrs in parser.attributes:
        codebase = codebase or attrs.get("codebase", "")
        archive = archive or attrs.get("archive", "")

    if not verify_value:
        raise RuntimeError("KVM page did not contain verifyValue.")
    if not type_data:
        raise RuntimeError("KVM page did not contain typeData.")

    codebase = codebase.strip().strip("/") or "jar"
    archive = archive.strip().split(",")[0].strip() or "vconsole.jar"
    return verify_value, mm_verify_value, type_data, f"{codebase}/{archive}"


def download_jar(
    session: requests.Session,
    base_url: str,
    jar_path: str,
    output: Path,
) -> None:
    jar_url = urllib.parse.urljoin(f"{base_url}/", jar_path)
    print(f"Downloading viewer from {jar_url}")
    payload = session.get(jar_url, timeout=60).content
    output.write_bytes(payload)

    if not zipfile.is_zipfile(output):
        preview = payload[:200].decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Downloaded file is not a valid JAR. The BMC returned: {preview!r}"
        )


def build_jnlp(
    destination: Path,
    *,
    ip: str,
    jar_name: str,
    verify_value: str,
    mm_verify_value: str,
    type_data: str,
    codebase_address: str,
) -> None:
    def esc(value: str) -> str:
        return html.escape(value, quote=True)

    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<jnlp spec="1.0+" codebase="{codebase_address}/">
  <information>
    <title>Huawei iMana Console - {esc(ip)}</title>
    <vendor>Huawei iMana</vendor>
    <offline-allowed/>
  </information>
  <resources>
    <j2se version="1.8*" max-heap-size="256m"/>
    <jar href="{esc(jar_name)}" main="true"/>
  </resources>
  <applet-desc
      name="Remote Virtual Console ({esc(ip)})"
      main-class="com.kvm.KVMApplet"
      width="950"
      height="700">
    <param name="verifyValue" value="{esc(verify_value)}"/>
    <param name="mmVerifyValue" value="{esc(mm_verify_value)}"/>
    <param name="typeData" value="{esc(type_data)}"/>
    <param name="java_arguments" value="-Xmx256m -Dcom.sun.net.ssl.checkRevocation=false"/>
    <param name="local" value="en"/>
    <param name="compress" value="0"/>
    <param name="vmm_compress" value="0"/>
    <param name="port" value="2198"/>
    <param name="vmmPort" value="8208"/>
    <param name="privilege" value="4"/>
    <param name="bladesize" value="1"/>
    <param name="productType" value="BMC"/>
    <param name="ietype" value="i586"/>
    <param name="IPA" value="{esc(ip)}"/>
    <param name="IPB" value="{esc(ip)}"/>
    <param name="title" value="IP: {esc(ip)}"/>
  </applet-desc>
  <security>
    <all-permissions/>
  </security>
</jnlp>
'''
    destination.write_text(content, encoding="utf-8")


def build_applet_html(jnlp: Path, destination: Path, codebase_address: str) -> None:
    root = ET.parse(jnlp).getroot()

    def elements_named(name: str):
        return (
            element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == name
        )

    applet = next(elements_named("applet-desc"), None)
    jar = next(elements_named("jar"), None)
    if applet is None or jar is None:
        raise RuntimeError(f"{jnlp} does not contain an applet and viewer JAR.")

    jar_href = jar.attrib.get("href")
    main_class = applet.attrib.get("main-class")
    if not jar_href or not main_class:
        raise RuntimeError(f"{jnlp} is missing the viewer JAR or applet main class.")

    def esc(value: str) -> str:
        return html.escape(value, quote=True)

    archive_url = urllib.parse.urljoin(f"{codebase_address}/", jar_href)
    parameters = "\n".join(
        f'  <param name="{esc(param.attrib.get("name", ""))}" value="{esc(param.attrib.get("value", ""))}">'
        for param in elements_named("param")
    )
    content = f'''<!doctype html>
<html><body>
<applet
    code="{esc(main_class)}"
    archive="{esc(archive_url)}"
    name="{esc(applet.attrib.get("name", "KVM console"))}"
    width="{esc(applet.attrib.get("width", "950"))}"
    height="{esc(applet.attrib.get("height", "700"))}">
{parameters}
</applet>
</body></html>
'''
    destination.write_text(content, encoding="utf-8")


def launch_appletviewer(viewer_url: str, policy: Path) -> None:
    if not JAVA8_APPLETVIEWER.is_file():
        raise RuntimeError(
            f"Java 8 appletviewer was not found at {JAVA8_APPLETVIEWER}. Install the Temurin 8 JDK first."
        )

    policy.write_text(
        "grant { permission java.security.AllPermission; };\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            str(JAVA8_APPLETVIEWER),
            f"-J-Djava.security.policy=={policy}",
            viewer_url,
        ],
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Java 8 appletviewer exited with status {result.returncode}"
        )


def start_static_server(directory: str, host="127.0.0.1", port=8000):
    directory = str(Path(directory).resolve())

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

    server = ThreadingHTTPServer((host, port), Handler)

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()

    return server


def main() -> int:
    server: ThreadingHTTPServer | None = None
    try:
        args = parse_args()
        ip = normalise_host(args.ip)
        cookie = args.cookie
        workdir = Path.cwd() / f"imana-kvm-{ip.replace(':', '_')}"
        workdir.mkdir(parents=True, exist_ok=True)
        jnlp_file = workdir / "imana-kvm.jnlp"

        print("starting web server")
        PORT = 9999
        server = start_static_server(str(workdir), port=PORT)
        host, port = server.server_address
        codebase_address = f"http://{host}:{port}"
        print(f"server started on {PORT}")

        base_url = f"https://{ip}"

        session = requests.Session()
        session.verify = False
        session.mount("https://", LegacyTLSAdapter())
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 Gecko/20100101 Firefox/102.0",
            }
        )

        if cookie:
            print("reusing session")
            session.cookies.set("session", cookie)
        else:
            password = args.password or getpass.getpass(
                f"Password for {args.username}@{ip}: "
            )
            print(f"Logging into {base_url} as {args.username}...")
            login(session, base_url, args.username, password)

        mode = 0 if args.shared else 1
        page = session.get(
            f"{base_url}/kvmvmm.asp?kvmmode={mode}&CertificateType=0"
        ).text

        verify_value, mm_verify_value, type_data, jar_path = extract_console_data(page)

        jar_name = Path(jar_path).name or "vconsole.jar"
        jar_file = workdir / jar_name

        download_jar(session, base_url, jar_path, jar_file)
        build_jnlp(
            jnlp_file,
            ip=ip,
            jar_name=jar_name,
            verify_value=verify_value,
            mm_verify_value=mm_verify_value,
            type_data=type_data,
            codebase_address=codebase_address,
        )
        print(f"Created {jnlp_file}")

        print("launching...")
        with tempfile.TemporaryDirectory(
            prefix=".appletviewer-", dir=workdir
        ) as launcher_directory:
            launcher_directory = Path(launcher_directory)
            viewer_html = launcher_directory / "viewer.html"
            policy = launcher_directory / "viewer.policy"
            relative_html = viewer_html.relative_to(workdir).as_posix()
            viewer_url = f"{codebase_address}/{urllib.parse.quote(relative_html)}"
            build_applet_html(jnlp_file, viewer_html, codebase_address)
            launch_appletviewer(viewer_url, policy)

    except KeyboardInterrupt:
        print("\nStopping server...")

    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            print("Server stopped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
