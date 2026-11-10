-- 005_admin_console.sql — admin console sign-in audit (2026-11-10, per Chase).
-- The admin console (admin/, Next.js) signs approved admins in with Google;
-- its server records sign-ins, denials and sign-outs here through
-- POST /v1/admin/api/auth-events. Additive only.

CREATE TABLE IF NOT EXISTS admin_auth_events (
  id BIGINT NOT NULL AUTO_INCREMENT,
  at DATETIME NOT NULL,
  event VARCHAR(32) NOT NULL,
  email VARCHAR(320) NULL,
  detail VARCHAR(255) NULL,
  PRIMARY KEY (id),
  KEY idx_aae_at (at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
