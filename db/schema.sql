-- life-game NPCレジストリ + 恋愛・結婚の年齢ガード(実装検証用)
--
-- 位置づけ: このDBは events.jsonl と競合する「もう一つの正」ではなく、
-- ログから都度再構築される「投影(projection)」として扱う(plan.md
-- 「恋愛・結婚の年齢ガードとプレイヤーキャラ除外」節を参照)。テーブルは
-- 破壊・再構築されうるものであり、「テーブルが無い」は「その仕組みが今は
-- 存在しない」という情報として扱ってよい。
--
-- ここで検証すること(opus第5回・第6回レビューの指摘への対応):
--   1. 恋愛対象になりうるNPCを、外部キー制約でプレイヤーキャラクターから
--      構造的に除外する(プロンプト指示ではなく、スキーマで保証する)
--   2. 年齢下限を、CHECK制約ではなくトリガーで検証する(CHECK制約は他
--      テーブルを参照できないため。SQLite/PostgreSQL/MySQLに共通の制限)
--   3. 年齢は誕生ターンからの導出値なので、世界時計(world_clock)と
--      合わせて都度計算する
--   4. チェックは双方向(NPC側・プレイヤーキャラ側の両方の年齢)で行う
--      (opus第5回レビュー「新規スポーンは意思決定年齢から始まる=プレイヤー
--      キャラ自身も未成年になりうる」という指摘への対応)
--   5. 安全機構は「family_mode」という独立した専用フラグで一括制御する
--      (2026-08-12再訂正)。一時、汎用の「世界のルールレジストリ」の1
--      エントリとして表現する案を試したが、複数の安全装置(恋愛の年齢制限・
--      未成年が絡む犯罪行為の排除等)を1つのフラグで束ねて連動させたい
--      という要望を受けて撤回した。単一の専用フラグの方が、複数の安全装置が
--      「オンかオフか」だけで同期でき、部分的な安全のズレ(片方だけ解除
--      してしまう等)が起きにくい。物語生成プロセスからは触れない、という
--      性質も、汎用レジストリの一部として"触れないと約束する"より、
--      構造的に別枠にしておく方が確実に守れる

-- --- 世界時計(投影。reduce_state()相当の処理で都度更新される想定) -------
CREATE TABLE world_clock (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- シングルトン行
    current_turn INTEGER NOT NULL DEFAULT 0
);
INSERT INTO world_clock (id, current_turn) VALUES (1, 0);

-- --- 安全機構の一括制御フラグ(family_mode) --------------------------------
-- 「この世界を実際に誰がプレイするか」(フィクション内の社会設定ではなく、
-- デプロイ・世界作成時の現実の判断)で、複数の安全装置をまとめてオン/オフする
-- マスタースイッチ。既定は安全側(true)。1人の成人だけがプレイする世界に
-- 限り、世界作成時に明示的にfalseへ変更してよい。物語生成プロセス
-- (時代生成・LLM主導のルール改廃)からは触れない、独立した専用フラグとする
-- (通常の社会規範=流行・常識・倫理の`reach`/`weight`とは別枠)。
--
-- このフラグを参照する安全装置(現在・将来分):
--   - 恋愛・結婚の年齢ガード(このファイルで実装済み、下記トリガー)
--   - 未成年が絡む犯罪行為の排除(未実装。犯罪メカニクス自体がまだ無いため。
--     実装する際はこのフラグを同様に参照すること)
CREATE TABLE world_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- シングルトン行
    family_mode BOOLEAN NOT NULL DEFAULT true
);
INSERT INTO world_settings (id, family_mode) VALUES (1, true);

-- 1ターン=1ヶ月、1年=12ターン(plan.md「世界の時間の流れ方」で決定済み)
CREATE OR REPLACE FUNCTION turns_to_years(turns INTEGER) RETURNS NUMERIC AS $$
    SELECT turns / 12.0;
$$ LANGUAGE sql IMMUTABLE;

-- --- NPC・プレイヤーキャラクターは別テーブル(外部キー制約の土台) --------
CREATE TABLE npcs (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    birth_turn INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE player_characters (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    birth_turn INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --- 恋愛・結婚レコード ---------------------------------------------------
-- npc_id は npcs テーブルのみを参照できる外部キー。player_characters を
-- 参照するレコードはスキーマ上そもそも作成不可能(プロンプト指示に頼らない)。
CREATE TABLE romantic_relationships (
    id SERIAL PRIMARY KEY,
    character_id INTEGER NOT NULL REFERENCES player_characters(id),
    npc_id INTEGER NOT NULL REFERENCES npcs(id),
    relationship_type TEXT NOT NULL CHECK (relationship_type IN ('dating', 'married')),
    started_turn INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --- 年齢ガード(トリガー。CHECK制約では他テーブルを参照できないため) ----
CREATE OR REPLACE FUNCTION check_ages_for_romance() RETURNS TRIGGER AS $$
DECLARE
    is_family_mode BOOLEAN;
    npc_birth INTEGER;
    char_birth INTEGER;
    cur_turn INTEGER;
    npc_age NUMERIC;
    char_age NUMERIC;
    min_age CONSTANT NUMERIC := 18;  -- 仮値。時代・社会の制度(結婚も制度の一つ)で
                                      -- 将来可変にする余地は残す
BEGIN
    SELECT family_mode INTO is_family_mode FROM world_settings WHERE id = 1;
    IF NOT is_family_mode THEN
        -- 1人の成人だけがプレイする世界として明示的に設定された場合のみスキップ。
        -- 既定値(true)ではこの分岐を通らない。
        RETURN NEW;
    END IF;

    SELECT current_turn INTO cur_turn FROM world_clock WHERE id = 1;
    SELECT birth_turn INTO npc_birth FROM npcs WHERE id = NEW.npc_id;
    SELECT birth_turn INTO char_birth FROM player_characters WHERE id = NEW.character_id;

    IF npc_birth IS NULL THEN
        RAISE EXCEPTION 'npc_id % が npcs テーブルに存在しません', NEW.npc_id;
    END IF;

    npc_age := turns_to_years(cur_turn - npc_birth);
    char_age := turns_to_years(cur_turn - char_birth);

    IF npc_age < min_age THEN
        RAISE EXCEPTION 'NPC(id=%)の年齢 %歳 が下限 %歳 未満です', NEW.npc_id, npc_age, min_age;
    END IF;
    IF char_age < min_age THEN
        RAISE EXCEPTION 'プレイヤーキャラクター(id=%)の年齢 %歳 が下限 %歳 未満です', NEW.character_id, char_age, min_age;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_check_ages_for_romance
    BEFORE INSERT ON romantic_relationships
    FOR EACH ROW
    EXECUTE FUNCTION check_ages_for_romance();

-- --- DROP/ALTER防止は、動的チェックではなくロールの所有権モデルで守る --------
-- 【2026-08-13訂正】当初、family_modeを都度クエリして判定するイベントトリガー
-- (sql_drop / ddl_command_end)でDROP/ALTERを防ごうとしたが、実装してみると
-- 自己参照のバグがあった: world_settings自体をDROPしようとすると、判定の
-- ために world_settings を問い合わせる時点で、その対象自体が既に(トランザクション
-- 内で)操作中のため正しく参照できない("relation does not exist"という誤った
-- エラーになる)。また関数のDROPも実装の不備で素通りしてしまっていた
-- (実測で発覚。`life-game/db/test_schema_protection.sql`参照)。
--
-- 動的な判定に頼らず、**PostgreSQLの所有権モデルそのものに守らせる**方が
-- 単純で堅牢: 「テーブルを作成したオーナー以外はDROP/ALTERできない」という
-- のはPostgreSQLの基本原則で、family_modeの値を問い合わせる必要すら無く、
-- 常時かかる。物語生成プロセス(アプリ本体)は、テーブルのオーナーではない
-- 制限ロール(lifegame_app)で接続させ、DROP/ALTER権限をそもそも持たせない。
-- 世界作成時のデプロイ判断(1人の成人だけがプレイする世界にする等)は、
-- オーナーロール(lifegame、docker-compose.ymlのPOSTGRES_USER)で行う。
CREATE ROLE lifegame_app NOLOGIN;
GRANT lifegame_app TO lifegame;  -- lifegameセッションから SET ROLE lifegame_app; で
                                  -- 制限ロールの挙動を検証できるようにする

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lifegame_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO lifegame_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO lifegame_app;
-- 今後このスキーマに追加されるテーブル・関数にも同じ権限を自動付与する
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lifegame_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO lifegame_app;
