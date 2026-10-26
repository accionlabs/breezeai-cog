"""PHP data access is recorded only on evidence (php/data_access.py)."""

from __future__ import annotations

import pytest

USER = (
    "<?php\nnamespace App\\Models;\nuse Illuminate\\Foundation\\Auth\\User as Authenticatable;\n"
    "class User extends Authenticatable {}\n"
)
SONG = "<?php\nnamespace App\\Models;\nuse Illuminate\\Database\\Eloquent\\Model;\nclass Song extends Model {}\n"
BASE = "<?php\nnamespace App\\Models;\nuse Illuminate\\Database\\Eloquent\\Model;\nabstract class Base extends Model {}\n"
TAG = "<?php\nnamespace App\\Models;\nclass Tag extends Base {}\n"
DTO = "<?php\nnamespace App\\Values;\nfinal class PlaylistCreateData { public static function make(): self {} }\n"


def _db(statements):
    return sorted((s.text.split("\n")[0].strip(), s.method, s.dataAccessHint)
                  for s in statements if s.semanticType == "db_method_call")


def _controller(body: str, imports: str = "") -> str:
    return (
        "<?php\nnamespace App\\Http\\Controllers;\nuse App\\Models\\User;\nuse App\\Models\\Song;\n"
        "use App\\Models\\Tag;\nuse App\\Values\\PlaylistCreateData;\n" + imports +
        "class C {\n    public function run(User $user) {\n" + body + "    }\n}\n"
    )


MODELS = {"app/Models/User.php": USER, "app/Models/Song.php": SONG, "app/Models/Base.php": BASE,
          "app/Models/Tag.php": TAG, "app/Values/PlaylistCreateData.php": DTO}


def test_eloquent_models_by_inheritance(analyze_files) -> None:
    stmts = analyze_files({**MODELS, "app/Http/Controllers/C.php": _controller(
        "        User::create(['name' => 'x']);\n"
        "        $songs = Song::query()->findMany([1, 2]);\n"
        "        Tag::where('a', 1)->get();\n"
        "        $user->save();\n"
    )})
    assert _db(stmts) == [
        ("$songs = Song::query()->findMany([1, 2]);", "findMany", "eloquent"),
        ("$user->save();", "save", "eloquent"),
        ("Tag::where('a', 1)->get();", "get", "eloquent"),
        ("User::create(['name' => 'x']);", "create", "eloquent"),
    ]


def test_non_models_and_non_data_methods_are_not_data_access(analyze_files) -> None:
    stmts = analyze_files({**MODELS, "app/Http/Controllers/C.php": _controller(
        "        PlaylistCreateData::make();\n"
        "        User::factory()->make();\n"
        "        Song::search('q')->take(5)->get();\n"
        "        User::observe(UserObserver::class);\n"
        "        $user->toArray();\n"
        "        $other->save();\n"
        "        collect(Schema::getIndexes('t'))->contains('x');\n"
        "        $this->client->search('q');\n"
        "        $queryBuilder->where('a', 1)->get();\n"
        "        $stmt->fetch();\n"
    )})
    assert _db(stmts) == []


def test_db_facade_needs_the_import_and_has_no_hint(analyze_files) -> None:
    stmts = analyze_files({"app/R.php": (
        "<?php\nnamespace App;\nuse Illuminate\\Support\\Facades\\DB;\n"
        "function f() { DB::table('songs')->where('a', 1)->get(); DB::raw('1')->get(); }\n"
    ), "app/S.php": "<?php\nnamespace App;\nfunction g() { DB::table('songs')->get(); }\n"})
    assert _db(stmts) == [("DB::table('songs')->where('a', 1)->get();", "get", None)]


def test_raw_sql_stays_a_query_statement(analyze_files) -> None:
    stmts = analyze_files({"app/R.php": (
        "<?php\nnamespace App;\nuse Illuminate\\Support\\Facades\\DB;\n"
        "function f() {\n    DB::select('SELECT id FROM users WHERE active = 1');\n}\n"
    )})
    assert _db(stmts) == []
    assert [s.semanticType for s in stmts if "SELECT" in s.text and s.startLine == 5] == ["query_statement"]


def test_doctrine_handles_and_repositories(analyze_files) -> None:
    stmts = analyze_files({
        "src/Controller/Blog.php": (
            "<?php\nnamespace App\\Controller;\nuse Doctrine\\ORM\\EntityManagerInterface;\n"
            "use App\\Repository\\PostRepository;\n"
            "class Blog {\n    public function __construct(private EntityManagerInterface $em) {}\n"
            "    public function save($post, PostRepository $posts) {\n"
            "        $this->em->persist($post);\n"
            "        $this->em->getRepository(Post::class)->findBy(['a' => 1]);\n"
            "        $posts->findOneBy(['slug' => 'x']);\n"
            "        $posts->findLatest();\n"
            "        $this->em->getRepository(Post::class);\n"
            "    }\n}\n"
        ),
        "src/Repository/PostRepository.php": (
            "<?php\nnamespace App\\Repository;\n"
            "use Doctrine\\Bundle\\DoctrineBundle\\Repository\\ServiceEntityRepository;\n"
            "class PostRepository extends ServiceEntityRepository {\n"
            "    public function findLatest() {\n"
            "        return $this->createQueryBuilder('p')->getQuery()->getResult();\n    }\n}\n"
        ),
    })
    assert _db(stmts) == [
        ("$posts->findOneBy(['slug' => 'x']);", "findOneBy", "doctrine"),
        ("$this->em->getRepository(Post::class)->findBy(['a' => 1]);", "findBy", "doctrine"),
        ("$this->em->persist($post);", "persist", "doctrine"),
        ("return $this->createQueryBuilder('p')->getQuery()->getResult();", "createQueryBuilder", "doctrine"),
    ]


def test_codeigniter_builder_and_models(analyze_files) -> None:
    stmts = analyze_files({
        "app/Models/ItemModel.php": (
            "<?php\nnamespace App\\Models;\nuse CodeIgniter\\Model;\nclass ItemModel extends Model {\n"
            "    public function items($id) {\n"
            "        $builder = $this->db->table('items');\n"
            "        $builder->where('item_id', $id);\n"
            "        return $builder->get()->getResult();\n    }\n}\n"
        ),
        "app/Controllers/Items.php": (
            "<?php\nnamespace App\\Controllers;\nuse App\\Models\\ItemModel;\n"
            "class Items {\n    public function index() {\n"
            "        $model = new ItemModel();\n        return $model->findAll();\n    }\n}\n"
        ),
        "app/Other.php": "<?php\nclass O { function f() { $this->db->table('x'); } }\n",
    })
    assert _db(stmts) == [
        ("$builder = $this->db->table('items');", "table", "codeigniter-builder"),
        ("$builder->where('item_id', $id);", "where", "codeigniter-builder"),
        ("return $builder->get()->getResult();", "get", "codeigniter-builder"),
        ("return $model->findAll();", "findAll", "codeigniter-builder"),
    ]


def test_ambiguous_class_parent_is_not_a_model(analyze_files) -> None:
    stmts = analyze_files({
        "a/Thing.php": "<?php\nnamespace App;\nuse Illuminate\\Database\\Eloquent\\Model;\nclass Thing extends Model {}\n",
        "b/Thing.php": "<?php\nnamespace App;\nclass Thing extends \\ArrayObject {}\n",
        "c.php": "<?php\nnamespace App;\nfunction f() { Thing::create([]); }\n",
    })
    assert _db(stmts) == []


@pytest.mark.parametrize("jobs", [1, 2])
def test_model_fact_reaches_workers(tmp_path, jobs) -> None:
    import gzip
    import json

    from breezeai_cog import analyze_repo

    repo = tmp_path / "repo"
    for rel, src in {**MODELS, "app/Http/Controllers/C.php": _controller("        User::create([]);\n")}.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
    result = analyze_repo(repo, capture_statements=True, out=tmp_path / "out", jobs=jobs)
    rec = next(json.loads(line) for line in gzip.open(result.out_path, "rt")
               if '"app/Http/Controllers/C.php"' in line)
    assert [(s["method"], s["dataAccessHint"]) for s in rec["statements"]
            if s.get("semanticType") == "db_method_call"] == [("create", "eloquent")]
