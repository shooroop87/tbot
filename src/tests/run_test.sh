#!/bin/bash
cd "$(dirname "$0")/.."
source venv/bin/activate
export $(grep -v '^#' .env.dev | xargs)

if [ -z "$1" ]; then
    echo "Использование: ./tests/run_test.sh <имя_теста>"
    echo ""
    echo "Доступные тесты:"
    ls -1 tests/test_*.py | sed 's/tests\//  /g' | sed 's/.py//g'
    exit 1
fi

python tests/$1.py