-- 010_device_login.sql — device-code sign-in for the claims CLI (2026-11-24, per
-- Chase; design: docs/cli-login.md). `claims login` starts a device code
-- (POST /v1/auth/device), an admin approves it in the console, and the CLI
-- exchanges it for a publisher token (POST /v1/auth/device/token). Only the
-- SHA-256 of the device code and of the token is stored. Additive only: the
-- previous build keeps working on the migrated database.

CREATE TABLE IF NOT EXISTS device_codes (
  device_code_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  user_code CHAR(8) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  client VARCHAR(64) NULL,
  host VARCHAR(255) NULL,
  ip VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  requested_at DATETIME NOT NULL,
  expires_at DATETIME NOT NULL,
  last_polled_at DATETIME NULL,
  decided_by VARCHAR(320) NULL,
  decided_at DATETIME NULL,
  token_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
  PRIMARY KEY (device_code_sha256),
  UNIQUE KEY idx_dc_user_code (user_code),
  KEY idx_dc_ip (ip, status, expires_at),
  KEY idx_dc_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS publisher_tokens (
  token_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  token_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  email VARCHAR(320) NOT NULL,
  host VARCHAR(255) NULL,
  created_at DATETIME NOT NULL,
  expires_at DATETIME NOT NULL,
  last_used_at DATETIME NULL,
  revoked_at DATETIME NULL,
  revoked_by VARCHAR(320) NULL,
  PRIMARY KEY (token_id),
  UNIQUE KEY idx_pt_hash (token_sha256),
  KEY idx_pt_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
