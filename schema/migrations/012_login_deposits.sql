-- 012_login_deposits.sql — deposits and object releases made with a `claims login`
-- token instead of a deposit key (2026-11-25, per Chase; docs/depositing-objects.md).
-- A deposit is made either by a key (key_id) or by a login (publisher_token_id,
-- publisher_email); the service writes exactly one of the two. An object release
-- records who opened and who closed it the same way: a key id (opened_by,
-- closed_by) or a login's token id and email.
--
-- Additive: QA and prod share this database and the previous build keeps running
-- on it. That build writes key_id and opened_by as before (both stay the same
-- type, only nullable now) and never reads the new columns; its deposit lists
-- join api_keys, so they leave login deposits out until the new build is deployed.
-- Apply this BEFORE deploying the build that accepts login deposits.
--
-- One effect per statement: on a re-run, 1060 (column exists) means "already
-- applied"; a MODIFY repeats harmlessly.

ALTER TABLE deposits MODIFY COLUMN key_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL;
ALTER TABLE deposits ADD COLUMN publisher_token_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL;
ALTER TABLE deposits ADD COLUMN publisher_email VARCHAR(320) NULL;

ALTER TABLE object_releases MODIFY COLUMN opened_by VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL;
ALTER TABLE object_releases ADD COLUMN opened_by_token_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL;
ALTER TABLE object_releases ADD COLUMN opened_by_email VARCHAR(320) NULL;
ALTER TABLE object_releases ADD COLUMN closed_by VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL;
ALTER TABLE object_releases ADD COLUMN closed_by_token_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL;
ALTER TABLE object_releases ADD COLUMN closed_by_email VARCHAR(320) NULL;
