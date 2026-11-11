# Image storage

The bucket root contains one directory per verified account namespace:

```text
<account storage key>/
  portrait                         # selected image UUID, plain text
  images/
    <image UUID>.jxl                # canonical image
    <image UUID>.avif               # legacy source, when present
    <image UUID>.webp               # legacy source, when present
    <image UUID>/variants/v1.webp    # regenerable delivery representation
    <image UUID>/variants/v1.avif
```

The account storage key remains the full SHA-256 of the verified issuer and
subject. Removing the redundant `accounts/` prefix changes neither identity
derivation nor authorization. Both the S3 transport and Worker R2 binding use
the same `ImageStore` key policy. Listings recognize only canonical/legacy
image keys belonging to the requesting account, excluding portrait pointers
and delivery variants.

## Cutover from the previous layout

This is a namespace change, with no legacy read fallback. Before deploying it
against an existing bucket, copy `accounts/<account storage key>/<suffix>` to
`<account storage key>/<suffix>`. Copy the complete account tree, including
portrait pointers, legacy originals and variants, preserving content type,
cache control, custom metadata and bytes. In particular, preserve dimensions,
channels, source digest and `created_at`; compare content hashes and metadata
before cutover. Listing order uses object modification time, so a copy can
change ordering/quota tie-breaking even if custom creation metadata survives;
check accounts near the quota before resuming uploads.

Pause image uploads, portrait selection and image deletions during the final
copy/verification and application switch so concurrent writes cannot be lost.
Retain the old objects while previously issued URLs are still in use and while
rollback is needed. Public URLs have no application expiry, so deleting their
old keys needs a deliberate retention/compatibility decision. For rollback,
reconcile writes made after cutover before switching back to the old release.

This source change does not copy or delete bucket objects, change bucket
policies, or deploy the application. Bucket policies, lifecycle rules and any
external consumers that explicitly name `accounts/` must be checked during
rollout. Removing a prefix simplifies keys; it does not create a new security
boundary or materially change the image service architecture.

## Missing-object review (2026-11-09)

This is a source review of Arctic and the explicitly permitted
`wright-pacific-one` checkout. No Pacific code or missing-object behavior was
changed. It does not establish how an uninspected intermediary account service
translates Arctic responses, or how deployed CDN/bucket policies behave.

### Arctic behavior

| Deleted object | Current result |
| --- | --- |
| Canonical JPEG XL, no legacy original | `GET /images/{id}` and by-source lookup return structured `404 image_not_found`. Listing and quota counting omit it, even if variants remain. |
| Canonical JPEG XL, legacy original remains | ID lookup reads the legacy original and recreates JPEG XL. By-source lookup still misses unless a canonical object with matching source metadata exists. |
| Delivery variant only | A fresh image lookup recreates it from the canonical bytes. An already-issued delivery URL can fail until regeneration happens. |
| Portrait pointer | Account portrait lookup returns `null`. Images themselves remain in the account library. |
| Original referenced by portrait pointer | Account portrait lookup catches `ImageNotFound` and returns `null`; the stale pointer remains. |
| Legacy original, canonical remains | JPEG XL remains authoritative; normal delivery continues. |

`S3ObjectStorage._invoke` maps missing-object responses to `ImageNotFound`;
`R2ObjectStorage.head/read` does the same for a missing binding result. The API
exception handler converts this to a stable 404. Infrastructure errors retain
their separate 502 status. Document persistence, CRDT reconstruction and history
do not fetch image objects, so an absent bucket file does not directly prevent
document reads or saves.

An exact-source upload can recreate a deleted canonical object with the same
account-scoped content ID. Portrait import also accepts a stable caller-selected
ID. Neither provides automatic recovery without source bytes. Orphan variants
do not count toward quota and `delete_image` first requires an original to
exist, so the normal delete endpoint cannot clean up a variants-only image.
Deleting an original does not revoke an existing variant or cached bytes.
Re-uploading an ID can reuse its surviving variant; future processing changes
must account for the versioned variant key.

There are expected races between HEAD, GET, conditional PUT and URL delivery.
For example, deletion after HEAD can produce 404 during conversion; deletion
after URL issuance can produce a successful metadata response followed by a
failed direct download. A successful metadata request is not a guarantee that
the subsequent byte fetch will succeed. Readiness probes only `.readiness`,
so readiness is intentionally unaffected by individual missing images.

### Pacific behavior

Relevant files are in `crates/` of `wright-pacific-one`:

- `wright-editor/src/image_menu.rs`: authenticated insertion downloads URL
  sources before upload, then stores an embedded data URL plus durable
  `{service, id}` attachment metadata. Offline insertion can retain an HTTP URL.
- `wright-rich-text/src/images.rs`: `src` is the document's display source;
  `attachment` holds service identity, never an expiring delivery URL.
- `wright-document/tests/images.rs`: existing serialization coverage retains
  embedded source, attachment and authored dimensions through save/reload.
- `wright-editor/src/images.rs`: the asset loader reads `src`, not the bucket
  attachment. Embedded images therefore survive bucket deletion. For URL-only
  images, a failed download shows a placeholder and Retry; the image node and
  authored size remain. Without an authored size, fallback geometry is
  320 by 200. Retry evicts the cached failure, then reloads the same URL; it
  cannot renew an expired signed URL or restore deleted bytes. A previously
  decoded asset can remain visible until its cache is released.
- `wright-backend/src/images.rs`: source-cache 404 triggers upload of the
  available bytes. Other failures remain upload errors and keep the draft.
  There is no general attachment download/repair resolver in the reviewed flow.
- `wright-ui/src/avatar.rs`: missing or undecodable portrait bytes fall back
  to initials. `wright-backend/src/account.rs` keeps identity live independently
  of portrait delivery; a null portrait schedules another attempt after 900
  seconds, while delivery errors retry after 10 seconds. This path uses the
  account API intermediary, not Arctic directly.
- `wright-markup/src/document.rs`: export uses `src`, so embedded sources do
  not depend on bucket availability. PDF export has a separate limitation:
  `wright-markup/src/pdf.rs` rejects all external file reads and reports compiler
  diagnostics. It has no missing-image placeholder policy; URL-backed image
  export cannot be assumed to succeed. This is separate from bucket deletion.

### Hardening plan (not implemented)

1. Define missing-image behavior as a normal asset state across metadata lookup
   and direct byte delivery. Preserve the document node, attachment ID, alt text
   and authored dimensions on failure. Distinguish a missing original from
   expired authorization and transient service failures. Do not infer permanent
   deletion from an arbitrary storage/CDN 403 or transport error.
2. Keep embedded bytes authoritative for existing documents. If attachments
   later become the primary source, introduce a shared account-scoped resolver
   before removing embedded bytes: refresh delivery credentials, fetch with
   bounds, preserve any local copy, and expose a stable missing-image state.
   Retry must resolve the durable attachment again rather than reuse a failed
   signed URL. Restoration should be explicit when source bytes are available;
   routine reads should not silently undo intentional deletions.
3. Make explicit image deletion idempotent across originals, legacy copies,
   variants and portrait references, including variants-only remnants. Define
   whether deletion means loss of the original or removal of all deliverable
   copies. Coordinate pointer replacement/cleanup so a concurrent portrait
   selection cannot be erased. Add bounded orphan reconciliation with a grace
   period and dry-run inventory; never use a broad prefix deletion.
4. Keep lazy regeneration for missing variants and add bounded handling for
   disappearance during regeneration. Treat missing originals as 404 without
   turning infrastructure failures into misses. Define how missing images
   appear in PDF/export and preflight them without blocking editing or saving.
5. Verify the contract with transport-level and service regression coverage:
   missing HEAD/GET for both adapters, deletion between HEAD and GET, deletion
   after conditional-write conflict, variant regeneration, canonical loss with
   surviving variants, legacy recovery, portrait pointer/image loss, repeated
   deletion, same-source re-upload, and cross-account isolation. In Pacific,
   verify save/reload after bucket loss, URL-only placeholder and retry, stable
   authored geometry, avatar fallback, and export diagnostics/placeholders.

Current conclusion: embedded document images and account presentation degrade
safely, and Arctic reports missing originals coherently. Full cleanup, remote
attachment recovery, and export behavior still need an explicit contract before
arbitrary bucket deletion can be considered safe throughout the project.
