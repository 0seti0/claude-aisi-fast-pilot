# Mechanical pilot notes

Study version: `fast-pilot-v11`
Manifest SHA-256: `6c694af1edeff896e38453a688cba7ecb7976bb7175eb8a4a55609ca4932396e`
Git commit: `3eb7e2fa945c523815675aead2fc6623e6ba3c36`; dirty: `False`

All counts are descriptive. Semantic manipulation codes remain `UNCODED` pending blinded review.

## Protocol telemetry

- target_model: parse failures 0/6; membership repair used 0/6; position-only rankings 0/6.
- control_model: parse failures 0/6; membership repair used 0/6; position-only rankings 0/6.

## Unique-clean coverage

- target_model: covers L, B, and S in `unique_clean`.
- control_model: covers L, B, and S in `unique_clean`.

## Baseline action counts by model and domain

- target_model × target: VERIFY=3
- target_model × neutral: VERIFY=3
- control_model × target: VERIFY=3
- control_model × neutral: VERIFY=3
