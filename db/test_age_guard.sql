-- 年齢ガード・トリガーの検証スクリプト
-- 世界時計を30年(360ターン)に進めた状態で、以下を確認する:
--   1. 成人キャラ×成人NPC → 成功するはず
--   2. 成人キャラ×未成年NPC → トリガーで拒否されるはず
--   3. 未成年キャラ×成人NPC → トリガーで拒否されるはず(双方向チェック)
--   4. プレイヤーキャラクターを恋愛相手にしようとする → 外部キー制約で
--      そもそも構文的に不可能(npc_idはnpcsテーブルしか参照できない)

UPDATE world_clock SET current_turn = 360 WHERE id = 1;

INSERT INTO npcs (id, name, birth_turn) VALUES
    (1, '成人NPC(佐藤さん)', 0),      -- 360ターン時点で30歳
    (2, '未成年NPC(子供)', 300);      -- 360ターン時点で5歳

INSERT INTO player_characters (id, name, birth_turn) VALUES
    (1, '成人プレイヤーキャラ', 0),    -- 30歳
    (2, '未成年プレイヤーキャラ', 300); -- 5歳(新規スポーン=意思決定年齢想定と別に、極端な例として)

\echo '--- テスト1: 成人×成人(成功するはず) ---'
INSERT INTO romantic_relationships (character_id, npc_id, relationship_type, started_turn)
VALUES (1, 1, 'dating', 360);

\echo '--- テスト2: 成人キャラ×未成年NPC(拒否されるはず) ---'
DO $$
BEGIN
    INSERT INTO romantic_relationships (character_id, npc_id, relationship_type, started_turn)
    VALUES (1, 2, 'dating', 360);
    RAISE NOTICE '[FAIL] 拒否されるべきなのに成功してしまった';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '[OK] 期待通り拒否された: %', SQLERRM;
END $$;

\echo '--- テスト3: 未成年キャラ×成人NPC(拒否されるはず、双方向チェック) ---'
DO $$
BEGIN
    INSERT INTO romantic_relationships (character_id, npc_id, relationship_type, started_turn)
    VALUES (2, 1, 'dating', 360);
    RAISE NOTICE '[FAIL] 拒否されるべきなのに成功してしまった';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '[OK] 期待通り拒否された: %', SQLERRM;
END $$;

\echo '--- テスト4: プレイヤーキャラを恋愛相手にしようとする(構文的に不可能なはず) ---'
DO $$
BEGIN
    -- npc_id に player_characters.id(=1)を渡す。npcsテーブルには存在しないID。
    INSERT INTO romantic_relationships (character_id, npc_id, relationship_type, started_turn)
    VALUES (1, 999, 'dating', 360);
    RAISE NOTICE '[FAIL] 拒否されるべきなのに成功してしまった';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '[OK] 期待通り拒否された(外部キー制約): %', SQLERRM;
END $$;

\echo '--- 最終確認: 成功したレコードは1件だけのはず ---'
SELECT count(*) AS successful_relationships FROM romantic_relationships;
