"""Verify that an OCI archive carries attached SPDX and SLSA attestations."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
from pathlib import Path
from typing import Any

ATTESTATION_TYPE = "application/vnd.docker.attestation.manifest.v1+json"
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
REQUIRED_PREDICATES = {
    "https://spdx.dev/Document",
    "https://slsa.dev/provenance/v1",
}
SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$")
MAX_DESCRIPTORS = 1024


class InvalidArchive(RuntimeError):
    """The archive is missing or misattaches required attestations."""


def _read_json(archive: tarfile.TarFile, member: str) -> dict[str, Any]:
    try:
        extracted = archive.extractfile(member)
    except KeyError as exc:
        raise InvalidArchive(f"missing OCI member {member}") from exc
    if extracted is None:
        raise InvalidArchive(f"OCI member {member} is not a regular file")
    try:
        value = json.load(extracted)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidArchive(f"OCI member {member} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise InvalidArchive(f"OCI member {member} must contain a JSON object")
    return value


def _blob_member(digest: object) -> str:
    match = SHA256.fullmatch(str(digest))
    if not match:
        raise InvalidArchive(f"invalid OCI descriptor digest {digest!r}")
    return f"blobs/sha256/{match.group(1)}"


def _verify_archive(archive: tarfile.TarFile) -> set[str]:
    root = _read_json(archive, "index.json")
    pending = list(root.get("manifests", []))
    seen: set[str] = set()
    image_manifests: set[str] = set()
    attestation_subjects: set[str] = set()
    predicates: set[str] = set()

    while pending:
        if len(seen) + len(pending) > MAX_DESCRIPTORS:
            raise InvalidArchive("OCI descriptor graph exceeds safety limit")
        descriptor = pending.pop()
        if not isinstance(descriptor, dict):
            raise InvalidArchive("OCI descriptor must be a JSON object")
        digest = str(descriptor.get("digest", ""))
        if digest in seen:
            continue
        seen.add(digest)
        document = _read_json(archive, _blob_member(digest))
        media_type = document.get("mediaType")

        if media_type == INDEX_TYPE:
            children = document.get("manifests", [])
            if not isinstance(children, list):
                raise InvalidArchive("OCI index manifests must be an array")
            pending.extend(children)
            continue

        if media_type != MANIFEST_TYPE:
            continue
        if document.get("artifactType") != ATTESTATION_TYPE:
            image_manifests.add(digest)
            continue

        subject = document.get("subject", {})
        if not isinstance(subject, dict):
            raise InvalidArchive("attestation subject must be an object")
        subject_digest = str(subject.get("digest", ""))
        _blob_member(subject_digest)
        attestation_subjects.add(subject_digest)

        layers = document.get("layers", [])
        if not isinstance(layers, list):
            raise InvalidArchive("attestation layers must be an array")
        for layer in layers:
            if not isinstance(layer, dict):
                raise InvalidArchive("attestation layer must be an object")
            annotations = layer.get("annotations", {})
            if isinstance(annotations, dict):
                predicate = annotations.get("in-toto.io/predicate-type")
                if isinstance(predicate, str):
                    predicates.add(predicate)

    unattached = attestation_subjects - image_manifests
    if unattached:
        raise InvalidArchive(
            "attestation subject is not an image manifest in the archive: "
            + ", ".join(sorted(unattached))
        )
    missing = REQUIRED_PREDICATES - predicates
    if missing:
        raise InvalidArchive(
            "missing required predicate(s): " + ", ".join(sorted(missing))
        )
    return predicates


def verify(path: Path) -> set[str]:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            return _verify_archive(archive)
    except (FileNotFoundError, tarfile.TarError) as exc:
        raise InvalidArchive(f"cannot open {path}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args()
    failed = False
    for path in args.archives:
        try:
            predicates = verify(path)
        except InvalidArchive as exc:
            failed = True
            print(f"{path}: {exc}", file=sys.stderr)
        else:
            required = ", ".join(sorted(REQUIRED_PREDICATES & predicates))
            print(f"{path}: attached predicates verified ({required})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
