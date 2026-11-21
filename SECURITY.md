# Security policy

imgstrip exists to stop images from giving things away, and to be safe to point at files you do not trust. A flaw
in either job is a security problem, and I would like to hear about it before anyone else does.

## What to report privately

* **A file that comes out of imgstrip still carrying metadata or hidden data, with every check passing.** The
  verifier is meant to refuse such a file; a case where it does not is the most valuable report there is. Please
  say which channel (the [reference](docs/REFERENCE.md) lists the ones imgstrip claims to handle and the ones it
  says it does not).
* **A way out of the decoder sandbox**, or a decoder crash or hang that the sandbox does not contain.
* **A way into the local web app** from another web page or another user of the same computer: getting past the
  `Host`, origin and token checks, reading a cleaned file without its link, or reaching the signing identity.
* **A leak in Content Credentials**: a signed file that reveals more about the source than the documentation says.
* Anything else you believe an attacker could use against someone running imgstrip.

## How to report

Use GitHub's private reporting: open the repository's **Security** tab and choose **Report a vulnerability**
(<https://github.com/lloydsato/imgstrip/security/advisories/new>). Please do not open a public issue for these, and
do not attach a hostile file to a public place. If you cannot use that form, open an issue that says only that you
have a security report and how you would like to be contacted, with no details.

A good report has the imgstrip version (`imgstrip --version`), the Python and Pillow versions, the command or call
you ran, and the smallest input that shows the problem. A file that triggers it is the most useful part.

## What to expect

You will get an answer, a fix or a clear reason why not, and credit in the changelog if you want it. A fix is
released as a new version and described in [CHANGELOG.md](CHANGELOG.md) once it is out.

## What the checks have found

A security test of 2.0.5 (fuzzing with coverage guidance, HEIF and AVIF decoders, a real browser, the Content Credentials code and a pass through the OWASP Top 10) found no way for hidden data to reach a cleaned file and no way out of the decoder sandbox. It found twelve smaller issues, all fixed in 2.0.6; [CHANGELOG.md](CHANGELOG.md) lists them.

## Supported versions

Only the latest release gets fixes. imgstrip requires a Pillow version without the known decoder flaws that matter
to it (see the changelog entries for 2.0.1 and 2.0.7); since 2.0.7 it refuses to start on an older one.

## Running it on files you do not trust

The decoders run in a separate, restricted process. How restricted depends on the system: on Linux 5.13 or later with
Landlock and seccomp it can read only the Python installation and write nothing, open no sockets and start no
programs; on older kernels, macOS and Windows it is a separate process with resource limits only, and imgstrip says
so once on standard error. To refuse to run with less than the full confinement, use `--require-sandbox` (or set
`IMGSTRIP_REQUIRE_SANDBOX=1`). `imgstrip --self-test` and the web app's `/api/info` show what your system gives.
`--accept jpeg,png` limits the formats that reach a decoder at all. On a computer shared with other users, start the
web app with `--no-browser`; it serves only your own user's connections unless you give `--any-user`. See
[Decoding in a sandbox](docs/REFERENCE.md#decoding-in-a-sandbox) for what each level does and does not stop.

## Processing records are sensitive

The optional processing record (`--record`, or *Keep a processing record* in the web app) names the original file and
holds its SHA-256, and it can list the details of what was removed (a location, say). Treat it as you treat the
original: keep it with the evidence, never publish it or send it with the cleaned image. imgstrip refuses to write it
into the folder the cleaned files go to, writes it readable by its owner only, and keeps it out of the web app's ZIP of
images and out of *Save to folder*. The signing key lives in memory only in the web app and is never written to its
folder. A record is only as trustworthy as the key that signed it: check records against a public key you know to be
the signer's (`--verify-record --record-pubkey`). See [Processing record](docs/REFERENCE.md#processing-record-optional)
for what it does and does not show.

## Out of scope

* Channels the documentation says imgstrip does not remove by default, such as pixel steganography without
  `--scrub-lsb` or `--scrub-alpha`. Reports that these channels exist are known; reports that a scrub fails to
  remove what it says it removes are welcome.
* Flaws in a library imgstrip uses (Pillow, libjpeg, libwebp, libheif and the like) that imgstrip cannot contain.
  Please report those to the library as well; tell me too if the sandbox does not stop them.
* Problems that need the attacker to be able to run code as the user already.
  (The app makes itself harder to read for such a process: no core dumps and no `ptrace` of the web app or of a
  process holding a signing key. That is a mitigation, not a boundary.)
