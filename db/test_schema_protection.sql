-- ロール分離によるDROP/ALTER防止の検証。
-- アプリ/物語生成の実行主体を模した制限ロール(lifegame_app)に切り替えてから
-- 保護対象オブジェクトのDROP/ALTERを試み、失敗することを確認する。
-- family_modeの値とは無関係に(PostgreSQLの所有権モデルそのものによって)
-- 常時防がれることも確認する。

SET ROLE lifegame_app;

\echo '--- テスト1: lifegame_appロールでDROP TABLE world_settings(拒否されるはず) ---'
DO $$
BEGIN
    DROP TABLE world_settings;
    RAISE NOTICE '[FAIL] 拒否されるべきなのに成功してしまった';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '[OK] 期待通り拒否された: %', SQLERRM;
END $$;

\echo '--- テスト2: lifegame_appロールでDROP FUNCTION check_ages_for_romance(拒否されるはず) ---'
DO $$
BEGIN
    DROP FUNCTION check_ages_for_romance() CASCADE;
    RAISE NOTICE '[FAIL] 拒否されるべきなのに成功してしまった';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '[OK] 期待通り拒否された: %', SQLERRM;
END $$;

\echo '--- テスト3: lifegame_appロールでALTER TABLE world_settings(拒否されるはず) ---'
DO $$
BEGIN
    ALTER TABLE world_settings ADD COLUMN dummy TEXT;
    RAISE NOTICE '[FAIL] 拒否されるべきなのに成功してしまった';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '[OK] 期待通り拒否された: %', SQLERRM;
END $$;

\echo '--- テスト4: lifegame_appロールでDROP TABLE npcs(拒否されるはず) ---'
DO $$
BEGIN
    DROP TABLE npcs CASCADE;
    RAISE NOTICE '[FAIL] 拒否されるべきなのに成功してしまった';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE '[OK] 期待通り拒否された: %', SQLERRM;
END $$;

\echo '--- テスト5: lifegame_appロールのまま、family_mode=false に変更を試みる(値の更新はDMLなので通るはず) ---'
UPDATE world_settings SET family_mode = false WHERE id = 1;
SELECT family_mode FROM world_settings WHERE id = 1;
UPDATE world_settings SET family_mode = true WHERE id = 1;  -- 後片付け

\echo '--- テスト6: lifegame_appロールのまま、通常のデータ操作(SELECT/INSERT)は通ること ---'
DO $$
BEGIN
    INSERT INTO npcs (name, birth_turn) VALUES ('テスト用NPC', 0);
    RAISE NOTICE '[OK] 通常のINSERTは成功する(データ操作は制限していない)';
END $$;

RESET ROLE;

\echo '--- テスト7: オーナーロール(lifegame)に戻すと、DROP/ALTERは通常どおり可能 ---'
DO $$
BEGIN
    CREATE TABLE dummy_table_for_test (id INTEGER);
    DROP TABLE dummy_table_for_test;
    RAISE NOTICE '[OK] オーナーロールでは無関係なテーブルのDROPが通常どおり動く(デプロイ判断はオーナーが行う想定)';
END $$;

\echo '--- 最終確認: 保護対象テーブル・関数がすべて残っていること ---'
SELECT table_name FROM information_schema.tables WHERE table_name IN ('world_settings', 'npcs') ORDER BY table_name;
SELECT proname FROM pg_proc WHERE proname = 'check_ages_for_romance';
