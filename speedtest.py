#!/usr/bin/env python3
"""
SpeedTest CLI — утилита для замера скорости интернет-соединения.

Выполняет последовательные HTTP/HTTPS запросы к указанному ресурсу,
замеряет время отклика и объём полученных данных, рассчитывает
среднее время запроса и итоговую скорость передачи данных.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import List, Optional

# Размер чанка при потоковом чтении тела ответа (64 KiB)
CHUNK_SIZE: int = 64 * 1024

# Стандартный User-Agent для идентификации запросов
DEFAULT_USER_AGENT: str = "SpeedTestCLI/1.0 (+https://github.com/airwar31/internet-speed-test)"


@dataclass
class RequestResult:
    """Результат выполнения одного сетевого запроса."""
    index: int
    status_code: int
    bytes_downloaded: int
    duration_seconds: float
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.error is None and 200 <= self.status_code < 300

    @property
    def speed_bytes_per_sec(self) -> float:
        if self.duration_seconds <= 0 or self.bytes_downloaded <= 0:
            return 0.0
        return self.bytes_downloaded / self.duration_seconds

    @property
    def speed_mb_per_sec(self) -> float:
        """Скорость в десятичных мегабайтах в секунду (1 MB/s = 10^6 B/s)."""
        return self.speed_bytes_per_sec / 1_000_000

    @property
    def speed_mib_per_sec(self) -> float:
        """Скорость в двоичных мебибайтах в секунду (1 MiB/s = 2^20 B/s)."""
        return self.speed_bytes_per_sec / (1024 * 1024)

    @property
    def speed_mbps(self) -> float:
        """Скорость в мегабитах в секунду (1 Mbps = 10^6 bit/s = 8 * 10^6 bit/s)."""
        return (self.speed_bytes_per_sec * 8) / 1_000_000


@dataclass
class BenchmarkSummary:
    """Итоговая статистика серии запросов."""
    url: str
    requested_count: int
    results: List[RequestResult]

    @property
    def successful_results(self) -> List[RequestResult]:
        return [r for r in self.results if r.is_success]

    @property
    def successful_count(self) -> int:
        return len(self.successful_results)

    @property
    def failed_count(self) -> int:
        return self.requested_count - self.successful_count

    @property
    def total_bytes(self) -> int:
        return sum(r.bytes_downloaded for r in self.successful_results)

    @property
    def total_download_time(self) -> float:
        return sum(r.duration_seconds for r in self.successful_results)

    @property
    def avg_request_time(self) -> float:
        if not self.successful_results:
            return 0.0
        return self.total_download_time / len(self.successful_results)

    @property
    def min_request_time(self) -> float:
        if not self.successful_results:
            return 0.0
        return min(r.duration_seconds for r in self.successful_results)

    @property
    def max_request_time(self) -> float:
        if not self.successful_results:
            return 0.0
        return max(r.duration_seconds for r in self.successful_results)

    @property
    def overall_speed_bytes_per_sec(self) -> float:
        if self.total_download_time <= 0 or self.total_bytes <= 0:
            return 0.0
        return self.total_bytes / self.total_download_time

    @property
    def overall_speed_mb_per_sec(self) -> float:
        """Итоговая скорость в MB/s (десятичные мегабайты, 10^6 байт/с)."""
        return self.overall_speed_bytes_per_sec / 1_000_000

    @property
    def overall_speed_mib_per_sec(self) -> float:
        """Итоговая скорость в MiB/s (двоичные мебибайты, 2^20 байт/с)."""
        return self.overall_speed_bytes_per_sec / (1024 * 1024)

    @property
    def overall_speed_mbps(self) -> float:
        """Итоговая скорость в Mbps (мегабиты в секунду, 10^6 бит/с)."""
        return (self.overall_speed_bytes_per_sec * 8) / 1_000_000

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "requested_count": self.requested_count,
            "successful_count": self.successful_count,
            "failed_count": self.failed_count,
            "total_bytes": self.total_bytes,
            "total_download_time_seconds": round(self.total_download_time, 4),
            "avg_request_time_seconds": round(self.avg_request_time, 4),
            "min_request_time_seconds": round(self.min_request_time, 4),
            "max_request_time_seconds": round(self.max_request_time, 4),
            "speed": {
                "mb_per_sec": round(self.overall_speed_mb_per_sec, 3),
                "mbps": round(self.overall_speed_mbps, 3),
                "mib_per_sec": round(self.overall_speed_mib_per_sec, 3),
                "bytes_per_sec": round(self.overall_speed_bytes_per_sec, 2),
            },
            "results": [asdict(r) for r in self.results],
        }


def format_bytes(num_bytes: int) -> str:
    """Форматирует размер данных в человекочитаемый вид (Б / КБ / МБ / ГБ)."""
    if num_bytes < 1024:
        return f"{num_bytes} Б"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.2f} КБ ({num_bytes / 1000:.2f} KB)"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} МиБ ({num_bytes / 1_000_000:.2f} МБ)"
    else:
        return f"{num_bytes / (1024 ** 3):.2f} ГиБ ({num_bytes / 1_000_000_000:.2f} ГБ)"


def validate_url(url: str) -> str:
    """Проверяет корректность URL и нормализует схему при необходимости."""
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("URL не может быть пустым.")
    parsed = urllib.parse.urlsplit(cleaned)
    if not parsed.scheme:
        # Если схема не указана, по умолчанию используем https://
        cleaned = f"https://{cleaned}"
        parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"Неподдерживаемый протокол '{parsed.scheme}'. Используйте http или https.")
    if not parsed.netloc:
        raise ValueError(f"Некорректный сетевой адрес в URL: '{url}'.")
    return cleaned


def perform_single_request(
    url: str,
    index: int,
    timeout: float = 30.0,
    ssl_context: Optional[ssl.SSLContext] = None,
    no_cache: bool = False,
) -> RequestResult:
    """
    Выполняет один запрос к целевому ресурсу и вычитывает тело ответа целиком.
    """
    target_url = url
    if no_cache:
        # Добавляем уникальный query параметр для обхода кэширования на промежуточных прокси/CDN
        delimiter = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{delimiter}_nocache={time.time_ns()}"

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Connection": "close",  # Запрещаем повторное использование TCP-сокета для чистоты замера
    }
    if no_cache:
        headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        headers["Pragma"] = "no-cache"

    req = urllib.request.Request(target_url, headers=headers, method="GET")

    bytes_received = 0
    status_code = 0
    t_start = time.perf_counter()

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as response:
            status_code = getattr(response, "status", 200)
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                bytes_received += len(chunk)

        t_end = time.perf_counter()
        duration = max(t_end - t_start, 1e-9)

        return RequestResult(
            index=index,
            status_code=status_code,
            bytes_downloaded=bytes_received,
            duration_seconds=duration,
            error=None,
        )

    except urllib.error.HTTPError as err:
        t_end = time.perf_counter()
        duration = max(t_end - t_start, 1e-9)
        # При ошибках HTTP (404, 500 и т.д.) читаем тело ошибки, если доступно
        try:
            body = err.read()
            bytes_received += len(body)
        except Exception:
            pass
        return RequestResult(
            index=index,
            status_code=err.code,
            bytes_downloaded=bytes_received,
            duration_seconds=duration,
            error=f"HTTP {err.code}: {err.reason}",
        )

    except urllib.error.URLError as err:
        t_end = time.perf_counter()
        duration = max(t_end - t_start, 1e-9)
        return RequestResult(
            index=index,
            status_code=0,
            bytes_downloaded=0,
            duration_seconds=duration,
            error=f"Сетевая ошибка: {err.reason}",
        )

    except TimeoutError:
        t_end = time.perf_counter()
        duration = max(t_end - t_start, 1e-9)
        return RequestResult(
            index=index,
            status_code=0,
            bytes_downloaded=bytes_received,
            duration_seconds=duration,
            error=f"Таймаут ожидания ответа ({timeout} с)",
        )

    except Exception as exc:
        t_end = time.perf_counter()
        duration = max(t_end - t_start, 1e-9)
        return RequestResult(
            index=index,
            status_code=0,
            bytes_downloaded=bytes_received,
            duration_seconds=duration,
            error=f"Ошибка: {exc}",
        )


def run_benchmark(
    url: str,
    count: int = 10,
    timeout: float = 30.0,
    ssl_context: Optional[ssl.SSLContext] = None,
    no_cache: bool = False,
    quiet: bool = False,
) -> BenchmarkSummary:
    """
    Выполняет заданное количество последовательных запросов к URL
    и возвращает агрегированную статистику.
    """
    results: List[RequestResult] = []
    cat_faces = ["(=^.^=)", "(=^･ｪ･^=)", "(=`ω´=)", "(=^..^=)", "(=^･ω･^=)"]

    if not quiet:
        print("=" * 80)
        print("             SpeedTest CLI — Замер скорости сети (=^･ω･^=)")
        print("=" * 80)
        print(f" Целевой URL         : {url}")
        print(f" Количество запросов : {count} (последовательно)")
        print(f" Таймаут на запрос   : {timeout:.1f} с")
        if no_cache:
            print(" Режим без кэша      : включён (Cache-Control + bust query)")
        print("-" * 80)
        sys.stdout.flush()

    for i in range(1, count + 1):
        res = perform_single_request(
            url=url,
            index=i,
            timeout=timeout,
            ssl_context=ssl_context,
            no_cache=no_cache,
        )
        results.append(res)

        if not quiet:
            cat = cat_faces[(i - 1) % len(cat_faces)]
            if res.is_success:
                size_mb = res.bytes_downloaded / 1_000_000
                print(
                    f" [{res.index:02d}/{count:02d}] {cat:<9} HTTP {res.status_code:<3} "
                    f"| Объём: {size_mb:6.2f} МБ "
                    f"| Время: {res.duration_seconds:6.3f} с "
                    f"| Скорость: {res.speed_mb_per_sec:6.2f} MB/s ({res.speed_mbps:6.2f} Mbps)"
                )
            else:
                print(
                    f" [{res.index:02d}/{count:02d}] {cat:<9} ОШИБКА: {res.error} "
                    f"| Время: {res.duration_seconds:6.3f} с"
                )
            sys.stdout.flush()

    return BenchmarkSummary(url=url, requested_count=count, results=results)


def print_summary(summary: BenchmarkSummary) -> None:
    """Выводит детальный итоговый отчёт в консоль с котиком."""
    print("-" * 80)
    print("ИТОГИ ТЕСТИРОВАНИЯ:")
    if summary.successful_count == 0:
        print("  /\\_/\\")
        print(" ( -.- )  Все запросы завершились с ошибкой. Замер скорости невозможен.")
        print("  > ^ <")
        print("=" * 80)
        return

    mb_val = summary.total_bytes / 1_000_000
    mib_val = summary.total_bytes / (1024 * 1024)

    print(f"  /\\_/\\    Успешных запросов       : {summary.successful_count} из {summary.requested_count}")
    print(f" ( o.o )   Общий объём данных      : {summary.total_bytes:,} байт (~{mb_val:.2f} МБ / {mib_val:.2f} МиБ)")
    print(f"  > ^ <    Суммарное время загрузки: {summary.total_download_time:.3f} с")
    print(f"           Среднее время запроса   : {summary.avg_request_time:.3f} с (мин: {summary.min_request_time:.3f} с, макс: {summary.max_request_time:.3f} с)")
    print()
    print("  СКОРОСТЬ СКАЧИВАНИЯ     :")
    print(f"    • {summary.overall_speed_mb_per_sec:>8.2f} MB/s   (Мегабайт в секунду, десятичные 10^6 Б/с)")
    print(f"    • {summary.overall_speed_mbps:>8.2f} Mbps   (Мегабит в секунду, сетевой стандарт 10^6 бит/с)")
    print(f"    • {summary.overall_speed_mib_per_sec:>8.2f} MiB/s  (Мебибайт в секунду, двоичные 2^20 Б/с)")
    print("=" * 80)


def create_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        prog="speedtest.py",
        description="Консольная утилита для измерения скорости интернета серией последовательных запросов.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры запуска:
  python speedtest.py https://speed.hetzner.de/10MB.bin
  python speedtest.py -u https://storage.yandexcloud.net/test/image.jpg -n 10
  python speedtest.py https://proof.ovh.net/files/10Mb.dat --no-cache
  python speedtest.py https://example.com/test.png --json
        """,
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="URL целевого файла или ресурса для замера скорости.",
    )
    parser.add_argument(
        "-u",
        "--url",
        dest="url_opt",
        default=None,
        help="Альтернативный флаг для указания URL.",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=10,
        help="Количество последовательных запросов (по умолчанию: 10).",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=30.0,
        help="Таймаут на каждый запрос в секундах (по умолчанию: 30.0).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Отключить кэширование (добавляет заголовки Cache-Control и антикэш-параметр).",
    )
    parser.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="Игнорировать проверку SSL-сертификатов (для отладки/локальных тестов).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Тихий режим: скрыть построчный вывод запросов, показать только итог.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести итоговый результат в формате JSON (удобно для скриптов и CI).",
    )
    return parser


def main() -> int:
    """Точка входа в приложение."""
    parser = create_parser()
    args = parser.parse_args()

    raw_url = args.url or args.url_opt
    if not raw_url:
        # Если URL не передан через аргументы, запрашиваем интерактивно
        try:
            raw_url = input("Введите адрес (URL) для проверки скорости: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nОтменено пользователем.")
            return 130

    if not raw_url:
        print("Ошибка: URL не указан. Запустите с флагом --help для справки.", file=sys.stderr)
        return 1

    try:
        url = validate_url(raw_url)
    except ValueError as err:
        print(f"Ошибка в параметре URL: {err}", file=sys.stderr)
        return 1

    if args.count <= 0:
        print("Ошибка: количество запросов (--count) должно быть больше 0.", file=sys.stderr)
        return 1

    ssl_context = None
    if args.insecure:
        ssl_context = ssl._create_unverified_context()
    else:
        ssl_context = ssl.create_default_context()

    try:
        summary = run_benchmark(
            url=url,
            count=args.count,
            timeout=args.timeout,
            ssl_context=ssl_context,
            no_cache=args.no_cache,
            quiet=args.quiet or args.json,
        )
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем (Ctrl+C).", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    else:
        print_summary(summary)

    if summary.successful_count == summary.requested_count:
        return 0
    elif summary.successful_count > 0:
        return 1
    else:
        return 2


if __name__ == "__main__":
    sys.exit(main())
