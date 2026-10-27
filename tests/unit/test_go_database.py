"""Go database access (parsers/go_database/access.py): db_method_call only on values bound to a
database library type."""

from __future__ import annotations

MOD = "module example.com/app\n\ngo 1.22\n"


def _db(analyze_files, imp: str, body: str):
    st = analyze_files({"go.mod": MOD, "main.go": f"package main\n\nimport (\n{imp}\n)\n\n" + body})
    return sorted((s.startLine, s.nodeType, s.method, s.dataAccessHint)
                  for s in st if s.semanticType == "db_method_call")


def test_database_sql(analyze_files) -> None:
    assert _db(analyze_files, '\t"database/sql"', 'func run() {\n\tdb, _ := sql.Open("postgres", dsn)\n'
                                                  '\trows, err := db.QueryContext(ctx, "SELECT id FROM users")\n'
                                                  '\t_, _ = rows, err\n\ttx, _ := db.Begin()\n\ttx.Exec("DELETE FROM t")\n}\n') \
        == [(9, "short_var_declaration", "QueryContext", "sql"), (12, "expression_statement", "Exec", "sql")]


def test_query_row_in_a_control_flow_header(analyze_files) -> None:
    assert _db(analyze_files, '\t"database/sql"', 'func run(db *sql.DB) {\n'
                                                  '\tif err := db.QueryRow("SELECT 1").Scan(&n); err != nil {\n\t}\n}\n') \
        == [(8, "short_var_declaration", "QueryRow", "sql")]  # the if's init statement; the header stays plain


def test_sqlx(analyze_files) -> None:
    assert _db(analyze_files, '\t"github.com/jmoiron/sqlx"', 'func run(db *sqlx.DB) {\n\tdb.Get(&u, "SELECT 1")\n'
                                                             '\tdb.NamedExec("INSERT", u)\n}\n') \
        == [(8, "expression_statement", "Get", "sqlx"), (9, "expression_statement", "NamedExec", "sqlx")]


def test_gorm_chain_is_one_record(analyze_files) -> None:
    assert _db(analyze_files, '\t"gorm.io/gorm"', 'type Repo struct{ db *gorm.DB }\n\n'
                                                  'func (r *Repo) find() {\n\tr.db.Where("email = ?", e).\n\t\tOrder("id").First(&u)\n'
                                                  '\tr.db.Raw("SELECT 1").Scan(&n)\n\tr.db.Model(&u).Updates(m)\n}\n') \
        == [(10, "expression_statement", "First", "gorm"), (12, "expression_statement", "Scan", "gorm"),
            (13, "expression_statement", "Updates", "gorm")]


def test_pgx_pool(analyze_files) -> None:
    assert _db(analyze_files, '\t"github.com/jackc/pgx/v5/pgxpool"', 'func run() {\n\tpool, _ := pgxpool.New(ctx, dsn)\n'
                                                                     '\tpool.QueryRow(ctx, "SELECT 1").Scan(&n)\n}\n') \
        == [(9, "expression_statement", "QueryRow", "pgx")]


def test_mongo_collection(analyze_files) -> None:
    assert _db(analyze_files, '\t"go.mongodb.org/mongo-driver/mongo"',
               'func run(client *mongo.Client) {\n\tcoll := client.Database("app").Collection("users")\n'
               '\tcoll.FindOne(ctx, filter)\n}\n') == [(9, "expression_statement", "FindOne", "mongodb")]


def test_redis_commands(analyze_files) -> None:
    assert _db(analyze_files, '\t"github.com/redis/go-redis/v9"',
               'func run() {\n\trdb := redis.NewClient(&redis.Options{})\n\trdb.Set(ctx, "k", 1, 0)\n'
               '\trdb.Get(ctx, "k")\n\trdb.Close()\n}\n') \
        == [(9, "expression_statement", "Set", "redis"), (10, "expression_statement", "Get", "redis")]


def test_names_alone_are_not_database_access(analyze_files) -> None:
    assert _db(analyze_files, '\t"net/http"', 'func run(client *http.Client, cache *Cache) {\n'
                                             '\tclient.Get("http://x")\n\tcache.Save("v")\n\tlist.Find(x)\n}\n') == []


def test_db_from_a_repo_function_in_another_package(analyze_files) -> None:
    st = analyze_files({
        "go.mod": MOD,
        "common/db.go": 'package common\n\nimport "gorm.io/gorm"\n\nfunc GetDB() *gorm.DB { return nil }\n',
        "main.go": ('package main\n\nimport "example.com/app/common"\n\n'
                    'func find() {\n\tdb := common.GetDB()\n\tdb.Where("x = ?", 1).Find(&out)\n}\n'),
    })
    assert [(s.startLine, s.method, s.dataAccessHint) for s in st if s.semanticType == "db_method_call"] \
        == [(7, "Find", "gorm")]


def test_name_based_guesses_are_not_made_for_go(analyze_files) -> None:
    assert _db(analyze_files, '\t"context"', 'func run(ctx context.Context) {\n\ta.articleRepo.Update(ctx, ar)\n'
                                            '\ts.cache.Get(key)\n\tdb.Query("SELECT 1")\n}\n') == []


def test_single_line_control_flow_keeps_no_semantic_type(analyze_files) -> None:
    st = analyze_files({"go.mod": MOD, "main.go": 'package main\n\nimport "gorm.io/gorm"\n\n'
                        'func run(db *gorm.DB) {\n\tif db.Find(&xs).Error != nil { return }\n'
                        '\tfor _, r := range db.Find(&ys).Rows() {}\n}\n'})
    flow = [s for s in st if s.nodeType in {"if_statement", "for_statement"}]
    assert flow and all(s.semanticType is None for s in flow)
    assert sorted((s.startLine, s.nodeType, s.method) for s in st if s.semanticType == "db_method_call") \
        == [(6, "call_expression", "Find"), (7, "call_expression", "Find")]  # Find runs the query
