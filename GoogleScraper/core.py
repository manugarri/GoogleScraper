# -*- coding: utf-8 -*-

import math
import threading
import queue
import datetime
from GoogleScraper.utils import grouper
from GoogleScraper import database
from GoogleScraper.database import ScraperSearch
from sqlalchemy.orm import scoped_session
from sqlalchemy.orm import sessionmaker
from GoogleScraper.proxies import parse_proxy_file, get_proxies_from_mysql_db
from GoogleScraper.scraping import SelScrape, HttpScrape
from GoogleScraper.caching import *
from GoogleScraper.config import InvalidConfigurationException, parse_cmd_args, Config
import GoogleScraper.config

logger = logging.getLogger('GoogleScraper')

def scrape_with_config(config, **kwargs):
    """Runs GoogleScraper with the dict in config.

    Args:
        config: A configuration dictionary that updates the global configuration.
        kwargs: Further options that cannot be handled by the configuration.
    Returns:
        The result of the main() function. May be void or a sqlite3 connection.
    """
    if not isinstance(config, dict):
        raise ValueError('The config parameter needs to be a configuration dictionary. Given parameter has type: {}'.format(type(config)))

    GoogleScraper.config.update_config(config)
    return main(return_results=True, force_reload=False, **kwargs)

def assign_keywords_to_scrapers(all_keywords):
    """Scrapers are often threads or asynchronous objects.

    Splitting the keywords equally on the workers is crucial
    for maximal performance.

    Args:
        all_keywords: All keywords to scrape

    Returns:
        A list of list. The inner list should be assigned to individual scrapers.
    """
    mode = Config['SCRAPING'].get('scrapemethod')


    if mode == 'sel':
        num_scrapers = Config['SELENIUM'].getint('num_browser_instances', 1)
    elif mode == 'http':
        num_scrapers = Config['HTTP'].getint('num_threads', 1)
    else:
        num_scrapers = 0


    if len(all_keywords) > num_scrapers:
        kwgroups = grouper(all_keywords, len(all_keywords)//num_scrapers, fillvalue=None)
    else:
        # thats a little special there :)
        kwgroups = [[kw, ] for kw in all_keywords]

    return kwgroups

def main(return_results=True):
    """Runs the GoogleScraper application as determined by the various configuration points.

    The main() function encompasses the core functionality of GoogleScraper. But it
    shouldn't be the main() functions job to check the validity of the provided
    configuration.

    Args:
        return_results: When GoogleScrape is used from within another program, don't print results to stdout,
                        store them in a database instead.
    Returns:
        A database connection to the results when return_results is True
    """
    parse_cmd_args()

    if Config['GLOBAL'].getboolean('view_config'):
        from GoogleScraper.config import CONFIG_FILE
        print(open(CONFIG_FILE).read())
        return

    maybe_clean_cache()

    kwfile = Config['SCRAPING'].get('keyword_file')
    keyword = Config['SCRAPING'].get('keyword')
    keywords = set(Config['SCRAPING'].get('keywords', '').split('\n'))
    proxy_file = Config['GLOBAL'].get('proxy_file', '')
    proxy_db = Config['GLOBAL'].get('mysql_proxy_db', '')

    if not (keyword or keywords) and not kwfile:
        raise InvalidConfigurationException('You must specify a keyword file (separated by newlines, each keyword on a line) with the flag `--keyword-file {filepath}~')

    if Config['GLOBAL'].getboolean('fix_cache_names'):
        fix_broken_cache_names()
        logger.info('renaming done. restart for normal use.')
        return

    keywords = [keyword, ] if keyword else keywords
    if kwfile:
        if not os.path.exists(kwfile):
            raise InvalidConfigurationException('The keyword file {} does not exist.'.format(kwfile))
        else:
            # Clean the keywords of duplicates right in the beginning
            keywords = set([line.strip() for line in open(kwfile, 'r').read().split('\n')])

    if Config['GLOBAL'].getboolean('check_oto', False):
        _caching_is_one_to_one(keyword)

    if Config['SCRAPING'].getint('num_results_per_page') > 100:
        raise InvalidConfigurationException('Not more that 100 results per page available for searches.')

    proxies = []

    if proxy_db:
        proxies = get_proxies_from_mysql_db(proxy_db)
    elif proxy_file:
        proxies = parse_proxy_file(proxy_file)

    valid_search_types = ('normal', 'video', 'news', 'image')
    if Config['SCRAPING'].get('search_type') not in valid_search_types:
        InvalidConfigurationException('Invalid search type! Select one of {}'.format(repr(valid_search_types)))

    if Config['GLOBAL'].getboolean('simulate', False):
        print('*' * 60 + 'SIMULATION' + '*' * 60)
        logger.info('If GoogleScraper would have been run without the --simulate flag, it would have')
        logger.info('Scraped for {} keywords (before caching), with {} results a page, in total {} pages for each keyword'.format(
            len(keywords), Config['SCRAPING'].getint('num_results_per_page', 0), Config['SCRAPING'].getint('num_pages_for_keyword')))
        logger.info('Used {} distinct proxies in total, with the following proxies: {}'.format(len(proxies), '\t\t\n'.join(proxies)))
        if Config['SCRAPING'].get('scrapemethod') == 'sel':
            mode = 'selenium mode with {} browser instances'.format(Config['SELENIUM'].getint('num_browser_instances'))
        else:
            mode = 'http mode'
        logger.info('By using scrapemethod: {}'.format(mode))
        return

    # First of all, lets see how many keywords remain to scrape after parsing the cache
    if Config['GLOBAL'].getboolean('do_caching'):
        remaining = parse_all_cached_files(keywords, None, url=Config['SELENIUM'].get('sel_scraper_base_url'))
    else:
        remaining = keywords

    kwgroups = assign_keywords_to_scrapers(remaining)

    scraper_search = ScraperSearch(
        number_search_engines_used=1,
        number_proxies_used=len(proxies),
        number_search_queries=len(keywords),
        started_searching=datetime.datetime.utcnow()
    )

    session_factory = sessionmaker(bind=database.engine)
    Session = scoped_session(session_factory)
    session = Session()

    # Let the games begin
    if Config['SCRAPING'].get('scrapemethod', 'http') == 'sel':
        # A lock to prevent multiple threads from solving captcha.
        lock = threading.Lock()
        rlock = threading.RLock()

        # Distribute the proxies evenly on the keywords to search for
        scrapejobs = []

        if Config['SCRAPING'].getboolean('use_own_ip'):
            proxies.append(None)
        elif not proxies:
            raise InvalidConfigurationException("No proxies available and using own IP is prohibited by configuration. Turning down.")

        chunks_per_proxy = math.ceil(len(kwgroups)/len(proxies))
        for i, keyword_group in enumerate(kwgroups):
            scrapejobs.append(SelScrape(keywords=keyword_group,rlock=rlock, session=session, captcha_lock=lock, browser_num=i, proxy=proxies[i//chunks_per_proxy]))

        for t in scrapejobs:
            t.start()

        for t in scrapejobs:
            t.join()

    elif Config['SCRAPING'].get('scrapemethod') == 'http':
        threads = []
        for group in kwgroups:
            threads.append(HttpScrape(keywords=group, session=session))

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

    elif Config['SCRAPING'].get('scrapemethod') == 'http_async':
        raise NotImplemented('soon my dead friends :)')

    else:
        raise InvalidConfigurationException('No such scrapemethod. Use "http" or "sel"')

    scraper_search.stopped_searching = datetime.datetime.utcnow()
    session.add(scraper_search)
    session.commit()