# coding: utf-8

from dtest import Tester
from assertions import *
from cql import ProgrammingError, OperationalError
from tools import *

import time
from threading import Thread
from ccmlib.cluster import Cluster

class TestPaxos(Tester):

    def prepare(self, ordered=False, create_keyspace=True, use_cache=False, nodes=1, rf=1):
        cluster = self.cluster

        if (ordered):
            cluster.set_partitioner("org.apache.cassandra.dht.ByteOrderedPartitioner")

        if (use_cache):
            cluster.set_configuration_options(values={ 'row_cache_size_in_mb' : 100 })

        cluster.populate(nodes).start()
        node1 = cluster.nodelist()[0]
        time.sleep(0.2)

        cursor = self.patient_cql_connection(node1, version="3.0.0").cursor()
        if create_keyspace:
            self.create_ks(cursor, 'ks', rf)
        return cursor

    @since('2.0.6')
    def contention_test_multi_iterations(self):
        self._contention_test(8, 100)

    @since('2.0.6')
    def contention_test_many_threds(self):
        self._contention_test(300, 1)

    def _contention_test(self, threads, iterations):
        """ Test threads repeatedly contending on the same row """

        verbose = False

        cursor = self.prepare(nodes=3)
        cursor.execute("CREATE TABLE test (k int, v int static, id int, PRIMARY KEY (k, id))")
        cursor.execute("INSERT INTO test(k, v) VALUES (0, 0)");

        class Worker(Thread):
            def __init__(self, wid, cursor, iterations, query):
                Thread.__init__(self)
                self.wid = wid
                self.iterations = iterations
                self.query = query
                self.cursor = cursor
                self.errors = 0
                self.retries = 0

            def run(self):
                global worker_done
                i = 0
                prev = 0
                while i < self.iterations:
                    done = False
                    while not done:
                        try:
                            self.cursor.execute_prepared(self.query, { 'new_value' : prev+1, 'prev_value' : prev, 'worker_id' : self.wid })
                            res = self.cursor.fetchall()
                            if verbose:
                                print "[%3d] CAS %3d -> %3d (res: %s)" % (self.wid, prev, prev+1, str(res))
                            if res[0][0] is True:
                                done = True
                                prev = prev + 1
                            else:
                                self.retries = self.retries + 1
                                # There is 2 conditions, so 2 reasons to fail: if we failed because the row with our
                                # worker ID already exists, it means we timeout earlier but our update did went in,
                                # so do consider this as a success
                                prev = res[0][3]
                                if res[0][2] is not None:
                                    if verbose:
                                        print "[%3d] Update was inserted on previous try (res = %s)" % (self.wid, str(res))
                                    done = True
                        except OperationalError as e:
                            if verbose:
                                print "[%3d] TIMEOUT (%s)" % (self.wid, str(e))
                            # This means a timeout: just retry, if it happens that our update was indeed persisted,
                            # we'll figure it out on the next run.
                            self.retries = self.retries + 1
                        except Exception as e:
                            if verbose:
                                print "[%3d] ERROR: %s" % (self.wid, str(e))
                            self.errors = self.errors + 1
                            done = True
                    i = i + 1
                    # Clean up for next iteration
                    while True:
                        try:
                            self.cursor.execute("DELETE FROM test WHERE k = 0 AND id = %d IF EXISTS" % self.wid)
                            break;
                        except OperationalError as e:
                            # Timeout, just retry
                            pass


        nodes = self.cluster.nodelist()
        workers = []
        for n in range(0, threads):
            c = self.cql_connection(nodes[n % len(nodes)], version="3.0.0").cursor()
            # This is overkill, we prepare the query multiple time per-host, but not a huge deal
            c.execute("USE ks")
            q = c.prepare_query("""
                BEGIN BATCH
                   UPDATE test SET v = :new_value WHERE k = 0 IF v = :prev_value;
                   INSERT INTO test (k, id) VALUES (0, :worker_id) IF NOT EXISTS;
                APPLY BATCH
            """)
            workers.append(Worker(n, c, iterations, q))

        start = time.time()

        for w in workers:
            w.start()

        for w in workers:
            w.join()

        if verbose:
            runtime = time.time() - start
            print "runtime:", runtime

        cursor.execute("SELECT v FROM test WHERE k = 0", consistency_level='ALL')
        value = cursor.fetchall()[0][0]

        errors = 0
        retries = 0
        for w in workers:
            errors = errors + w.errors
            retries = retries + w.retries

        assert (value == threads * iterations) and (errors == 0), "value=%d, errors=%d, retries=%d" % (value, errors, retries)

