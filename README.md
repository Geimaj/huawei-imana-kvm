# Huawei iMana KVM launcher

A small Python launcher for the legacy Java remote console found on Huawei
RH2288H V2 servers with iMana management firmware.

Modern browsers no longer support Java applets, so the iMana web interface
cannot launch its KVM console directly. This tool logs in to iMana, retrieves
the console parameters and viewer JAR, creates the required applet definition,
and opens it with the `appletviewer` included in a Java 8 JDK.

## What it does

When run, the launcher:

1. Connects to the server's iMana HTTPS interface.
2. Logs in using the supplied username and a securely prompted password (or
   reuses a supplied session cookie).
3. Downloads the KVM viewer JAR from the BMC.
4. Starts a temporary local HTTP server on `127.0.0.1:9999`.
5. Builds the applet metadata needed by the Huawei viewer.
6. Opens the console using Java 8 `appletviewer`.

The local server stops when the viewer exits.

## Requirements

This project currently targets **macOS** and requires:

- Python 3.13 or newer
- [`uv`](https://docs.astral.sh/uv/) (recommended for installing and running
  the Python dependency)
- Eclipse Temurin JDK 8, including `appletviewer`
- Network access to:
  - the iMana HTTPS interface (normally TCP 443)
  - the KVM service (TCP 2198)
  - the virtual-media service if used (TCP 8208)

The script expects `appletviewer` at:

```text
/Library/Java/JavaVirtualMachines/temurin-8.jdk/Contents/Home/bin/appletviewer
```

Install Java 8 and `uv` with Homebrew:

```bash
brew install --cask temurin@8
brew install uv
```

> A current Java release such as Java 17 or 21 is not a substitute. Java
> applets and `appletviewer` are legacy technologies, and this launcher needs
> the Java 8 JDK specifically.

## Installation

Clone the repository, enter it, and let `uv` create the Python environment:

```bash
uv sync
```

## Running the console

Pass the iMana IP address (or hostname) and username. If `--password` is
omitted, the launcher prompts for it without displaying it:

```bash
uv run python open_kvm.py 192.0.2.10 Administrator
```

To request a shared console session instead of the default exclusive session:

```bash
uv run python open_kvm.py 192.0.2.10 Administrator --shared
```

All options:

```text
usage: open_kvm.py [-h] [--password PASSWORD] [--cookie COOKIE] [--shared]
                   [--keep]
                   ip username

positional arguments:
  ip                   iMana IP address or hostname
  username             iMana username

options:
  --password PASSWORD  Password; omit to enter securely
  --cookie COOKIE      Reuse an active iMana session cookie
  --shared             Use shared KVM mode
  --keep               Keep JAR and JNLP files
```

Avoid putting a password directly on the command line: it may be saved in shell
history or exposed in the process list. The interactive prompt is safer.

## Generated files

The launcher creates a directory named `imana-kvm-<host>` in the current
directory. It stores the downloaded viewer JAR and generated JNLP file there.


## Security note

Old iMana firmware uses obsolete TLS settings and commonly presents an
untrusted certificate. To remain compatible, the launcher disables certificate
verification, permits legacy TLS/ciphers, and grants the downloaded Java applet
all permissions. Only use it with a trusted BMC.