import requests
from googlesearch import search
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import time
import argparse
import sys
import logging
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from dataclasses import dataclass
from typing import Dict, Set, Optional
from concurrent.futures import ThreadPoolExecutor
import threading

DEFAULT_QUERIES = {
    "FY 2024 DoD Budget": "DoD budget FY 2024 spending filetype:pdf site:*.edu | site:*.org | site:*.gov -inurl:(signup | login)",
    "FY 2025 DoD Budget": "DoD budget FY 2025 spending filetype:pdf site:*.edu | site:*.org | site:*.gov -inurl:(signup | login)",
    "FY 2024/2025 DoD Vendor Spending": "DoD vendor spending FY 2024 OR FY 2025 filetype:pdf site:*.edu | site:*.org | site:*.gov -inurl:(signup | login)"
}

@dataclass
class Config:
    timeout: int = 10
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    max_retries: int = 3
    backoff_factor: float = 1.5
    retry_status_codes: tuple = (429, 500, 502, 503, 504)
    search_results_limit: int = 15
    sleep_interval: float = 0.5
    max_workers: int = 5

class SessionManager:
    def __init__(self, config: Config):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.user_agent})
        retry_strategy = Retry(
            total=config.max_retries,
            backoff_factor=config.backoff_factor,
            status_forcelist=config.retry_status_codes
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

class PDFSearcher:
    def __init__(self, session: requests.Session, config: Config):
        self.session = session
        self.config = config
        self.lock = threading.Lock()

    def find_pdf_links(self, query: str, verbose: bool = False) -> Set[str]:
        pdf_links = set()
        if verbose:
            logging.info(f"Searching: {query}")
        
        try:
            search_results = list(search(query, num_results=self.config.search_results_limit))
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                executor.map(lambda url: self._process_url(url, pdf_links, verbose), search_results)
        except Exception as e:
            logging.error(f"Search failed: {str(e)}")
        
        return pdf_links

    def _process_url(self, url: str, pdf_links: Set[str], verbose: bool) -> None:
        try:
            if url.endswith(".pdf"):
                self._check_direct_pdf(url, pdf_links, verbose)
            else:
                self._scrape_page_for_pdfs(url, pdf_links, verbose)
        finally:
            time.sleep(self.config.sleep_interval)

    def _check_direct_pdf(self, url: str, pdf_links: Set[str], verbose: bool) -> None:
        try:
            response = self.session.head(url, timeout=self.config.timeout, allow_redirects=True)
            if response.status_code == 200:
                with self.lock:
                    pdf_links.add(url)
                if verbose:
                    logging.debug(f"Found: {url}")
        except requests.RequestException:
            pass

    def _scrape_page_for_pdfs(self, url: str, pdf_links: Set[str], verbose: bool) -> None:
        try:
            response = self.session.get(url, timeout=self.config.timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            pdf_urls = [urljoin(url, link["href"]) for link in soup.find_all("a", href=True) 
                       if link["href"].endswith(".pdf")]
            
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                executor.map(lambda pdf_url: self._check_direct_pdf(pdf_url, pdf_links, verbose), pdf_urls)
        except (requests.RequestException, ValueError) as e:
            if verbose:
                logging.debug(f"Skipping {url}: {str(e)}")

class FileHandler:
    @staticmethod
    def save_results(filename: str, search_results: Dict[str, Set[str]]) -> None:
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"Search performed on: {time.ctime()}\n\n")
                for title, links in search_results.items():
                    f.write(f"{title} ({len(links)} results):\n")
                    for link in sorted(links):
                        f.write(f"{link}\n")
                    f.write("\n")
            logging.info(f"Results saved to {filename}")
        except IOError as e:
            logging.error(f"Failed to save results: {str(e)}")

class SearchApplication:
    def __init__(self):
        self.config = Config()
        self.session_manager = SessionManager(self.config)
        self.searcher = PDFSearcher(self.session_manager.session, self.config)

    def run(self) -> None:
        args = self._parse_args()
        self._setup_logging(args.verbose)
        search_results = self._perform_searches(args)
        self._save_results(args.output, search_results)

    def _parse_args(self) -> argparse.Namespace:
        parser = argparse.ArgumentParser(description="Search for DoD spending PDFs")
        parser.add_argument("-o", "--output", default=None, help="Output file name")
        parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed progress")
        parser.add_argument("-q", "--queries", nargs="*", help="Custom queries as 'Title:query'")
        return parser.parse_args()

    def _setup_logging(self, verbose: bool) -> None:
        logging.basicConfig(
            level=logging.DEBUG if verbose else logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)]
        )

    def _get_queries(self, args: argparse.Namespace) -> Dict[str, str]:
        if not args.queries:
            return DEFAULT_QUERIES
        
        queries = {}
        for q in args.queries:
            try:
                title, query = q.split(":", 1)
                queries[title.strip()] = query.strip()
            except ValueError:
                logging.error(f"Invalid query format: {q}. Use 'Title:query'")
                sys.exit(1)
        return queries

    def _perform_searches(self, args: argparse.Namespace) -> Dict[str, Set[str]]:
        queries = self._get_queries(args)
        search_results = {}
        
        with ThreadPoolExecutor(max_workers=min(len(queries), self.config.max_workers)) as executor:
            future_to_title = {
                executor.submit(self.searcher.find_pdf_links, query, args.verbose): title 
                for title, query in queries.items()
            }
            
            for future in future_to_title:
                title = future_to_title[future]
                logging.info(f"{title}")
                try:
                    pdf_links = future.result()
                    search_results[title] = pdf_links
                    for index, pdf in enumerate(sorted(pdf_links), 1):
                        logging.info(f"[{index}] {pdf}")
                    if not pdf_links:
                        logging.info("No PDFs found")
                except Exception as e:
                    logging.error(f"Failed to process {title}: {str(e)}")
        
        return search_results

    def _save_results(self, output: Optional[str], search_results: Dict[str, Set[str]]) -> None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = output or f"dod_spending_pdfs_{timestamp}.txt"
        FileHandler.save_results(filename, search_results)

if __name__ == "__main__":
    SearchApplication().run()
