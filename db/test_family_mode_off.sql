-- family_mode フラグ(複数の安全装置を束ねるマスタースイッチ)が false の場合に、
-- 年齢ガードがバイパスされることを確認する検証スクリプト。
-- 前提: test_age_guard.sql 実行後の状態を引き継ぐ想定だが、単独実行でも動くよう
-- 必要なデータを再投入する(ON CONFLICTで重複を無視)。

INSERT INTO npcs (id, name, birth_turn) VALUES (2, '未成年NPC(子供)', 300)
    ON CONFLICT (id) DO NOTHING;
INSERT INTO player_characters (id, name, birth_turn) VALUES (1, '成人プレイヤーキャラ', 0)
    ON CONFLICT (id) DO NOTHING;

\echo '--- family_mode=true(既定)のまま: 未成年NPCとの関係は拒否されるはず ---'
DO $$
BEGIN
    INSERT INTO romantic_relationships (character_id, npc_id, relationship_type, started_turn)
    VALUES (1, 2, 'dating', 360);
    RAISE NOTICE '[FAIL] 拒否されるべきなのに成功してしまった';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '[OK] 期待通り拒否された: %', SQLERRM;
END $$;

\echo '--- family_mode を false に明示変更(マスタースイッチをオフに) ---'
UPDATE world_settings SET family_mode = false WHERE id = 1;

\echo '--- family_mode=false: 同じ未成年NPCとの関係が今度は通るはず(ガード解除の確認) ---'
DO $$
BEGIN
    INSERT INTO romantic_relationships (character_id, npc_id, relationship_type, started_turn)
    VALUES (1, 2, 'dating', 360);
    RAISE NOTICE '[OK] family_mode=falseでガードが解除され、成功した';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '[FAIL] family_mode=falseなのに拒否されてしまった: %', SQLERRM;
END $$;

\echo '--- 後片付け: family_mode を既定値(true)に戻す ---'
UPDATE world_settings SET family_mode = true WHERE id = 1;
