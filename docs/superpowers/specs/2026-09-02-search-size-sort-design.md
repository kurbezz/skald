# Search size sorting design

## Goal

Let users sort search results by release size in addition to seeders and leechers.

## Approach

Extend the existing server-side `sort` query parameter with the `size_bytes` field. The default page order remains seeders descending. Selecting Size starts with the largest releases first; selecting the active Size header toggles between descending and ascending order.

## Interface

The existing Size column header becomes a sort link. It preserves `q` and `type`, uses the same active visual state and arrow as the peer headers, and sets `aria-sort` on the active Size header only. Selecting another sort field continues to start in descending order.

## Validation and errors

`size_bytes` joins the existing sort-field allowlist. Unsupported field or direction values continue to reset the complete sort state to `seeders desc`.

## Testing

Route integration tests use releases with distinct byte sizes to assert size descending and ascending order. They also assert the Size header link, initial descending action, toggled ascending action, and active `aria-sort` state.
