CREATE TABLE IF NOT EXISTS weight_sets (
    id TEXT PRIMARY KEY, weight_set_key TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO weight_sets VALUES
('target_equivalent','target_equivalent',1,'{"id":"target_equivalent","key":"target_equivalent","title":"Еквівалент цілей","revision":1}',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')),
('complexity','complexity',1,'{"id":"complexity","key":"complexity","title":"Складність","revision":1}',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')),
('priority','priority',1,'{"id":"priority","key":"priority","title":"Пріоритет","revision":1}',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')),
('cost_equivalent','cost_equivalent',1,'{"id":"cost_equivalent","key":"cost_equivalent","title":"Еквівалент витрат","revision":1}',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'));
ALTER TABLE category_weights RENAME TO legacy_category_weights;
CREATE TABLE category_weights (
    id TEXT PRIMARY KEY, weight_set_id TEXT NOT NULL REFERENCES weight_sets(id) ON DELETE RESTRICT,
    category TEXT NOT NULL, revision INTEGER NOT NULL,
    definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(weight_set_id, category)
);
INSERT INTO category_weights (id,weight_set_id,category,revision,definition_json,created_at,updated_at)
SELECT id,'target_equivalent',category,revision,definition_json,created_at,updated_at FROM legacy_category_weights;
DROP TABLE legacy_category_weights;
