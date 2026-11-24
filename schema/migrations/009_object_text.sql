-- 009_object_text.sql — the text index behind GET /v1/objects/search?q=.
--
-- Until now `q` ran LIKE over three JSON slots of every stored object, twice
-- (count, then page): a scan of the whole of dapper_objects that took 13 to 40
-- seconds at 6.9 million objects. object_text holds, per object and text field
-- (statement, name, description), that field's words lowercased (service/
-- text_index.py is the one definition); a FULLTEXT index narrows the rows and
-- LIKE over `words` decides each match.
--
-- Additive: nothing existing changes. Rows are written with each object from
-- now on; scripts/migrate.py backfills the objects already stored.
-- `words` is utf8mb4_bin: it is already lowercased, and a bin collation keeps
-- LIKE and the index byte-exact.

CREATE TABLE IF NOT EXISTS object_text (
  object_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  field VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  class_name VARCHAR(64) NOT NULL,
  words TEXT NOT NULL,
  PRIMARY KEY (object_id, field),
  KEY idx_ot_class (class_name),
  FULLTEXT KEY ft_ot_words (words)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
