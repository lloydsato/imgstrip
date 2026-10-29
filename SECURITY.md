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

## Supported versions

Only the latest release gets fixes. imgstrip requires a Pillow version without the known decoder flaws that matter
to it (see the changelog entry for 2.0.1); if you must run an older one, keep the decoder sandbox on.

## Out of scope

* Channels the documentation says imgstrip does not remove by default, such as pixel steganography without
  `--scrub-lsb` or `--scrub-alpha`. Reports that these channels exist are known; reports that a scrub fails to
  remove what it says it removes are welcome.
* Flaws in a library imgstrip uses (Pillow, libjpeg, libwebp, libheif and the like) that imgstrip cannot contain.
  Please report those to the library as well; tell me too if the sandbox does not stop them.
* Problems that need the attacker to be able to run code as the user already.
