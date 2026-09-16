# exitzero

**AI가 “끝났다”고 말해도, 검사를 통과하기 전에는 끝난 것이 아닙니다.**
정책을 실행하고, 종료 코드를 확인하고, 영수증을 남기세요.

[English](README.md) | 한국어

exitzero는 저장소의 정책을 실행하는 작은 개발 도구입니다. 같은 TOML 정책으로
로컬 CLI, Git 훅, CI를 검사하고 매번 JSON 실행 영수증을 남깁니다. 사용자는
하나의 명령을 쓰고, 내부는 작은 코어와 플러그인으로 나뉩니다. 이 MVP는
Python 코드와 에이전트 설정을 검사합니다. 명시적인 command 검사로는 어떤
언어든 기존 도구를 그대로 실행할 수 있습니다.

## 설치

Python 3.11 이상이 필요합니다. 런타임과 테스트는 표준 라이브러리만
사용합니다.

```sh
pip install exitzero
```

이후 아무 저장소 안에서:

```sh
exitzero init
exitzero check
exitzero lint-config
exitzero report --format json
```

설치하지 않고 쓰려면 모든 명령 앞에 `uvx`를 붙이세요 (`pipx run`도 가능):

```sh
uvx exitzero init
uvx exitzero check
```

`init`은 시작용 TOML 정책, `AGENTS.md`의 관리 섹션, 로컬 영수증과 Python
캐시용 ignore 항목을 만듭니다. 빈 저장소에서는 작은 Python 샘플도 함께
만듭니다. 기존 정책 파일은 보존합니다. 시작 정책은 구문만 검사하므로, 머지
게이트로 쓰기 전에 프로젝트별 검사를 추가하세요. 구문 통과는 애플리케이션이
동작한다는 주장이 아닙니다.

소스 체크아웃에서 바로 시험해볼 수도 있습니다 — 패키지 설치나 빌드 도구
없이:

```sh
git clone https://github.com/ictechgy/exitzero.git
cd exitzero
export PATH="$PWD/bin:$PATH"
```

일반적인 소스 설치는 가상환경에서 `python -m pip install .`을 사용합니다.
빌드에는 setuptools를 씁니다. setuptools와 wheel이 이미 있다면 오프라인
editable 설치는 `python -m pip install --no-index --no-build-isolation
--no-deps -e .`입니다. 위의 체크아웃 런처는 빌드 도구가 필요 없습니다.

## Python 저장소용 검사 생성

`init`이 흔한 정적 검사들과 기존 테스트/리뷰 명령을 한 번에 작성해줍니다.
위의 인자 없는 `init` *대신* 이 명령을 실행하세요 — `init`은 기존 정책을
절대 덮어쓰지 않고 정책이 있으면 2로 종료합니다. 이미 정책이 있다면 직접
수정한 뒤 `exitzero init --sync`를 실행하세요. 각 명령은 argv로 파싱되고
나중에 `shell=False`로 실행됩니다. `{python}`은 `exitzero`를 실행 중인
Python 인터프리터를 뜻합니다.

```sh
exitzero init --profile python \
  --source-root src --source-root tests \
  --allow-module numpy --allow-module pytest \
  --test-command '{python} -m pytest' \
  --review-command '{python} scripts/review_contract.py'
```

`--source-root`, `--allow-module`, `--review-command`는 반복할 수 있습니다.
구두점이 포함된 인자는 따옴표로 감싸세요. 셸 파이프라인, 리다이렉트 등 제어
연산자는 거부됩니다. `init`은 명령을 기록만 하므로 실행하지 않습니다.
정책이 결과 argv를 저장하므로 명령 인자에 자격증명을 넣지 마세요. 기존
정책은 절대 덮어쓰지 않으며, 생성 옵션은 `init --sync`와 함께 쓸 수
없습니다. `--profile python` 없이는 `init`은 구문 전용 호환 시작점으로
유지됩니다. 생성된 command 검사는 영수증을 위해 Python 파일을
핑거프린트합니다. 테스트나 리뷰 명령이 JSON, YAML, Markdown 등 비-Python
입력에 의존한다면 정책에서 해당 검사의 `paths`에 그 파일들을 포함하세요.

체크아웃에서는 포함된 예제가 네 가지 검사 종류를 모두 실행합니다:

```sh
./bin/exitzero --root examples/sample check
./bin/exitzero --root examples/sample lint-config
```

## 정책에 리뷰 요구사항 담기

```toml
version = 1
plugins = ["exitzero_verify", "exitzero_harness"]

[[checks]]
id = "syntax"
kind = "python.syntax"
paths = ["src/**/*.py", "tests/**/*.py"]

[[checks]]
id = "imports"
kind = "python.imports"
paths = ["src/**/*.py", "tests/**/*.py"]
[checks.options]
roots = ["src", "."]
allow_modules = []

[[checks]]
id = "test-quality"
kind = "python.test-quality"
paths = ["tests/test_*.py"]

[[checks]]
id = "review-contracts"
kind = "command"
paths = ["src/**/*.py", "tests/**/*.py"]
[checks.options]
argv = ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]
timeout = 30

[harness]
config_files = []
rules = [{id = "error-text", value = "Tests assert the exact public error text."}]
```

정책을 수정한 뒤에는 `exitzero init --sync`를 실행합니다. `AGENTS.md`의
생성 섹션만 바뀝니다. 핑거프린트가 파싱된 정책 전체를 커버하므로 검사 옵션을
바꿔도 감지 가능한 드리프트가 생깁니다. 섹션 밖의 텍스트는 그대로입니다.
Harness 규칙은 문서화와 충돌 감지이지 의미적 강제가 아닙니다. 정확한
메시지, 타입, 결과 순서는 실행 가능한 테스트로 표현하세요.
[샘플](examples/sample)을 참고하세요.

| 검사 | v1이 감지하는 것 |
| --- | --- |
| `python.syntax` | 파싱할 수 없는 Python |
| `python.imports` | 해결되지 않는 모듈과 정적으로 선언된 로컬 모듈 심볼 누락 |
| `python.test-quality` | 테스트 케이스 없음, 빈 테스트, 상수만 있는 자명한 단언 |
| `command` | 설정된 테스트/린트 명령의 실패 또는 타임아웃 초과 |
| Harness lint | 생성된 AGENTS 드리프트, 설치된 훅 드리프트, JSON/TOML 설정 형태 오류(Cursor·Claude 훅 문서, MCP 서버 테이블), 중복/충돌 규칙 ID |

import 분석은 임포트된 코드를 실행하지 않습니다. 의도적으로 보수적이며 임의의
동적 export, 패키지 로딩, 서드파티 API 시그니처를 증명하지 않습니다.
`allow_modules`는 나열된 외부 모듈명을 명시적으로 신뢰합니다.
test-quality 분석은 명백한 문제만 감지하므로 실제 테스트도 함께
실행하세요. 임의의 AGENTS 산문이나 Cursor 규칙 파일의 자연어 모순은 v1이
이해하지 못합니다.

## 명령과 결과

```sh
exitzero init
exitzero init --sync
exitzero check --format json
exitzero lint-config --format json
exitzero hooks install --adapter cursor
exitzero hooks install --adapter pre-commit
exitzero hooks run --slot CI --format json
exitzero report --format json
exitzero plugin harness-eval --scenario examples/eval-repair   # 선택적 바운디드 eval
exitzero plugin mcp-gateway --config gateway.toml              # stdio MCP 프록시
```

전역 `--root`와 `--policy` 옵션은 서브커맨드 앞에 옵니다. `check`는 설정
린터와 검증 검사를 실행합니다. `lint-config`는 검증 명령을 절대 실행하지
않습니다. `plugin harness-eval`은 스크립트된 멀티턴 시나리오를 임시
사본에서 게이트에 리플레이합니다. 턴별 기대값을 채점하고, 스킵된
시나리오는 따로 보고하며, 리포트는 `.exitzero/evals/`에 남습니다.
[eval 예제](examples/eval-repair)를 참고하세요. `plugin mcp-gateway`는
업스트림 MCP 서버 하나를 서브프로세스로 띄워 stdio JSON-RPC를
프록시합니다. `tools/call`은 TOML allow/deny 패턴으로 인가되고(기본 거부)
모든 결정이 `.exitzero/mcp-gateway/` 감사 로그에 남습니다. 설정 스키마는
[플러그인 계약](docs/PLUGIN_API.md)을 참고하세요.

게이트 명령 — `check`, `lint-config`, `hooks run` — 은 종료 코드로 결과를
보고합니다:

| 종료 코드 | 의미 |
| --- | --- |
| 0 | 설정된 검사가 모두 통과 |
| 1 | 검사가 위반을 발견 (실패·타임아웃·시작 불가 명령 포함) |
| 2 | 잘못된 정책, 플러그인 누락, 내부 실행 오류, 또는 영수증 기록 실패 |

`init`과 `report`는 게이트가 아닙니다. `init`은 옵션이 잘못됐거나 기존
정책이 있으면 2로 종료하고, `report`는 저장된 최신 영수증을 출력하며 그
영수증 자체가 실행 오류를 기록했을 때만 2로 종료합니다.

모든 `check`, `lint-config`, 훅 게이트는 실패와 잘못된 정책을 포함해
`.exitzero/runs/` 아래 고유한 JSON 영수증을 남깁니다. 저장에 실패하면 명령은
2로 종료하고 `receipt: null`을 보고합니다 — 성공을 주장할 수 없습니다.
`--format json`은 같은 기계 판독 결과를 출력합니다. `report`는 최신
영수증을 읽을 뿐 새 검사를 실행하지 않습니다.
[영수증 스키마](docs/receipt.schema.json)를 참고하세요.

영수증에는 검사 ID, 발견 항목, 종료 코드, 정책 해시, 선택된 입력의 해시,
플러그인 이름, 소요 시간이 포함됩니다. 소스 코드, 환경 변수, 훅 입력, 명령
인자, 명령 출력은 포함하지 않습니다. 명령 출력은 버려지므로 실패한 명령은
직접 다시 실행해 디버깅하세요. 영수증은 선언된 입력 선택을 식별할 뿐 임의
명령의 모든 의존성을 나타내지 않습니다. 서명되거나 변조 방지된 증명이 아닌
로컬 증거입니다.

## 로컬 훅과 CI

[훅 설정](docs/HOOKS.md)을 참고하세요. Cursor는 기본으로 `stop` 훅을
사용합니다 — 실패 시 후속 수정 턴을 한 번 요청합니다. 이것은 피드백이지
머지 차단 장벽이 아닙니다. Git pre-commit과 CI는 종료 코드를 강제합니다.
범용 훅 명령은 `check`와 같은 0/1/2 계약을 따르며, Cursor는 결과를 자체
JSON 프로토콜로 번역합니다.

exitzero 체크아웃 루트에서 저장소의 자동화된 마일스톤 러너를 실행하세요:

```sh
python3 scripts/ci.py
```

저장소 게이트, 설정 린트, 샘플 게이트, 매니페스트로 채점되는 fixture를
실행하고 `.exitzero/ci-results.json`과 로그를 남깁니다. GitHub 워크플로는
같은 스크립트를 실행하며 실패 시에도 `.exitzero/` 증거를 업로드합니다.
호스팅 서비스에서 CI 잡을 필수 브랜치 검사로 설정하세요 — 이 저장소는
브랜치 보호 설정을 변경하지 않습니다.

[riskgate 파일럿](docs/PILOT_RISKGATE.md)은 같은 게이트를 고정된 실제
저장소에 적용합니다. 통과하는 베이스라인과 네 개의 독립 결함을 검사하는데,
여기에는 업스트림 테스트 러너가 그대로 통과시키는 빈 테스트도 포함됩니다.
러너는 원본 체크아웃을 보존하고 모든 게이트 영수증을 기록합니다.
[vecdiff 파일럿](docs/PILOT_VECDIFF.md)은 외부 NumPy 의존성과 독립적인
수치 리뷰 계약을 추가합니다. 두 파일럿 모두 격리된 소스 사본을 씁니다.

## 신뢰와 범위

정책, 선택된 플러그인, command 검사는 신뢰된 실행 가능 설정입니다.
낯선 저장소에서는 실행 전에 검토하세요. 정적 검사와 설정 린트는 네트워크
요청을 하지 않습니다. command 검사는 임의의 로컬 프로그램을 실행할 수
있으므로, 게이트 전체를 오프라인으로 유지하려면 오프라인 명령을 고르세요.
이것은 실행 샌드박스가 아니며, 로드맵에 있는 MCP allowlist 게이트웨이도
아닙니다.
`harness.config_files`에 나열된 설정 파일은 명시적으로 선택될 때만
읽습니다. 설치된 Cursor 훅 파일은 드리프트 감지를 위해 자동으로
핑거프린트됩니다. 자격증명 파일을 포함하지 마세요. 알려진 자격증명
유사 경로와 심볼릭 링크 대상은 거부됩니다. 경로 필터링은 범용 시크릿
탐지기가 아닙니다.

v1에는 클라우드 서비스, 모델 호스팅, 모델 학습, 전체 에이전트 평가,
프록시 게이트웨이, PR 발행기, 자동 롤백이 없습니다.

## 구조와 기여

```text
packages/core              정책, CLI, 훅 슬롯, 플러그인 로더, 영수증
packages/plugin-verify     검증 규칙과 command 검사
packages/plugin-harness    설정 린트; eval 명령 스텁
packages/plugin-mcp-gateway  v1.2 인터페이스 스텁
packages/plugin-ledger       v1.3 인터페이스 스텁
fixtures/                  매니페스트 채점 케이스 (라이브 전용 스킵 1개 선언)
examples/sample/           실행 가능한 오류 텍스트/타입/순서 계약 예제
```

[Plugin API](docs/PLUGIN_API.md), [로드맵](ROADMAP.md),
[설계 참고문헌](docs/REFERENCES.md)을 읽어주세요. 이전 기술에서 복사한
프로젝트 코드는 없습니다. 집중 회귀 테스트는 `python3 scripts/run_tests.py`,
전체 로컬 수용 시퀀스는 `python3 scripts/ci.py`로 실행합니다. 빌드된 패키지와 실제 Git 훅 동작은 [오프라인 릴리스
러너](docs/RELEASE.md)로 검증합니다. [릴리스 노트](CHANGELOG.md)를
참고하세요.
라이선스: [MIT](LICENSE).
