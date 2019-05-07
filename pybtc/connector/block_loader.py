import asyncio
import os
from multiprocessing import Process
from pybtc.functions.tools import int_to_bytes, bytes_to_int
from concurrent.futures import ThreadPoolExecutor
from setproctitle import setproctitle
import logging
import signal
import sys
import aiojsonrpc
import traceback
import pickle

class BlockLoader:
    def __init__(self, parent, workers=4):
        self.worker_limit = workers
        self.worker = dict()
        self.worker_tasks = list()
        self.worker_busy = dict()
        self.parent = parent
        self.loading_task = None
        self.log = parent.log
        self.loop = parent.loop
        self.rpc_url = parent.rpc_url
        self.rpc_timeout = parent.rpc_timeout
        self.rpc_batch_limit = parent.rpc_batch_limit
        self.loop.set_default_executor(ThreadPoolExecutor(workers * 2))
        self.watchdog_task = self.loop.create_task(self.watchdog())


    async def watchdog(self):
        while True:
            try:
                if self.loading_task is None or self.loading_task.done():
                    if self.parent.deep_synchronization:
                        self.loading_task = self.loop.create_task(self.loading())
                else:
                    pass
                    # clear tail

            except asyncio.CancelledError:
                self.log.info("connector watchdog terminated")
                break
            except Exception as err:
                self.log.error(str(traceback.format_exc()))
                self.log.error("watchdog error %s " % err)
            await asyncio.sleep(60)


    async def loading(self):
        self.worker_tasks = [self.loop.create_task(self.start_worker(i)) for i in range(self.worker_limit)]
        target_height = self.parent.node_last_block - self.parent.deep_sync_limit
        height = self.parent.last_block_height + 1
        self.log.info(str(height))
        while height < target_height:
            new_requests = 0
            if self.parent.block_preload._store_size < self.parent.block_preload_cache_limit:
                try:
                    for i in self.worker_busy:
                        if not self.worker_busy[i]:
                            self.worker_busy[i] = True
                            if height <= self.parent.last_block_height:
                                height = self.parent.last_block_height + 1
                            self.pipe_sent_msg(self.worker[i].writer, b'get', int_to_bytes(height))
                            height += self.rpc_batch_limit
                            new_requests += 1
                    if not new_requests:
                        await asyncio.sleep(1)
                except asyncio.CancelledError:
                    self.log.info("Loading task terminated")
                    break
                except Exception as err:
                    self.log.error("Loading task  error %s " % err)
            else:
                await  asyncio.sleep(1)



    async def start_worker(self,index):
        self.log.warning('Start block loader worker %s' % index)
        # prepare pipes for communications
        in_reader, in_writer = os.pipe()
        out_reader, out_writer = os.pipe()
        in_reader, out_reader  = os.fdopen(in_reader,'rb'), os.fdopen(out_reader,'rb')
        in_writer, out_writer  = os.fdopen(in_writer,'wb'), os.fdopen(out_writer,'wb')

        # create new process
        worker = Process(target=Worker, args=(index, in_reader, in_writer, out_reader, out_writer,
                                              self.rpc_url, self.rpc_timeout, self.rpc_batch_limit))
        worker.start()
        in_reader.close()
        out_writer.close()
        # get stream reader
        worker.reader = await self.get_pipe_reader(out_reader)
        worker.writer = in_writer
        worker.name   = str(index)
        self.worker[index] =  worker
        self.worker_busy[index] =  False
        # start message loop
        self.loop.create_task(self.message_loop(self.worker[index]))
        # wait if process crash
        await self.loop.run_in_executor(None, worker.join)
        del self.worker[index]
        self.log.warning('Block loader worker %s is stopped' % index)



    async def get_pipe_reader(self, fd_reader):
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            await self.loop.connect_read_pipe(lambda: protocol, fd_reader)
        except:
            return None
        return reader

    async def pipe_get_msg(self, reader):
        while True:
            try:
                msg = await reader.readexactly(1)
                if msg == b'M':
                    msg = await reader.readexactly(1)
                    if msg == b'E':
                        msg = await reader.readexactly(4)
                        c = int.from_bytes(msg, byteorder='little')
                        msg = await reader.readexactly(c)
                        if msg:
                            return msg[:20].rstrip(), msg[20:]
                if not msg:
                    return b'pipe_read_error', b''
            except:
                return b'pipe_read_error', b''

    def pipe_sent_msg(self, writer, msg_type, msg):
        msg_type = msg_type[:20].ljust(20)
        msg = msg_type + msg
        msg = b''.join((b'ME', len(msg).to_bytes(4, byteorder='little'), msg))
        writer.write(msg)
        writer.flush()



    async def message_loop(self, worker):
        while True:
            msg_type, msg = await self.pipe_get_msg(worker.reader)
            if msg_type ==  b'pipe_read_error':
                if not worker.is_alive():
                    return
                continue

            if msg_type == b'result':
                msg
                continue


    # def disconnect(self,ip):
    #     """ Disconnect peer """
    #     p = self.out_connection_pool[self.outgoing_connection[ip]["pool"]]
    #     pipe_sent_msg(p.writer, b'disconnect', ip.encode())





class Worker:

    def __init__(self, name , in_reader, in_writer, out_reader, out_writer,
                 rpc_url, rpc_timeout, rpc_batch_limit):
        setproctitle('Block loader: worker %s' % name)
        self.rpc_url = rpc_url
        self.rpc_timeout = rpc_timeout
        self.rpc_batch_limit = rpc_batch_limit
        self.name = name

        in_writer.close()
        out_reader.close()
        policy = asyncio.get_event_loop_policy()
        policy.set_event_loop(policy.new_event_loop())
        self.loop = asyncio.get_event_loop()
        self.log = logging.getLogger("Block loader")
        self.log.setLevel(logging.INFO)
        self.loop.set_default_executor(ThreadPoolExecutor(20))
        self.out_writer = out_writer
        self.in_reader = in_reader
        signal.signal(signal.SIGTERM, self.terminate)
        self.loop.create_task(self.message_loop())
        self.loop.run_forever()

    async def message_loop(self):
        try:
            self.rpc = aiojsonrpc.rpc(self.rpc_url, self.loop, timeout=self.rpc_timeout)
            self.reader = await self.get_pipe_reader(self.in_reader)
            while True:
                msg_type, msg = await self.pipe_get_msg(self.reader)
                if msg_type ==  b'pipe_read_error':
                    return

                if msg_type == b'get':
                    self.log.critical(str(bytes_to_int(msg)))
                    continue
        except:
            self.log.critical("exc")



    def terminate(self,a,b):
        sys.exit(0)

    async def get_pipe_reader(self, fd_reader):
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            await self.loop.connect_read_pipe(lambda: protocol, fd_reader)
        except:
            return None
        return reader

    async def pipe_get_msg(self, reader):
        while True:
            try:
                self.log.critical("---1")
                msg = await reader.readexactly(1)
                self.log.critical("---2")
                if msg == b'M':
                    msg = await reader.readexactly(1)
                    if msg == b'E':
                        msg = await reader.readexactly(4)
                        c = int.from_bytes(msg, byteorder='little')
                        msg = await reader.readexactly(c)
                        if msg:
                            return msg[:20].rstrip(), msg[20:]
                if not msg:
                    return b'pipe_read_error', b''
            except:
                return b'pipe_read_error', b''

    def pipe_sent_msg(self, writer, msg_type, msg):
        msg_type = msg_type[:20].ljust(20)
        msg = msg_type + msg
        msg = b''.join((b'ME', len(msg).to_bytes(4, byteorder='little'), msg))
        writer.write(msg)
        writer.flush()




"""

                        batch = list()
                        h_list = list()
                        while True:
                            batch.append(["getblockhash", height])
                            h_list.append(height)
                            if len(batch) >= self.rpc_batch_limit or height >= max_height:
                                height += 1
                                break
                            height += 1
                        result = await self.rpc.batch(batch)
                        h = list()
                        batch = list()
                        for lh, r in zip(h_list, result):
                            try:
                                self.block_hashes.set(lh, r["result"])
                                batch.append(["getblock", r["result"], 0])
                                h.append(lh)
                            except:
                                pass
                        self.log.critical(">>>")
                        blocks = await self.block_loader.load_blocks(batch)

                        for x,y in zip(h,blocks):
                            try:
                                self.block_preload.set(x, y)
                            except:
                                pass
                    except asyncio.CancelledError:
                        self.log.info("connector preload_block_hashes failed")
                        break
                    except:
                        pass

                if processed_height < self.last_block_height:
                    for i in range(processed_height, self.last_block_height ):
                        try:
                            self.block_preload.remove(i)
                        except:
                            pass
                    processed_height = self.last_block_height
                if self.block_preload._store and next(iter(self.block_preload._store)) <  processed_height + 1:
                    for i in range(next(iter(self.block_preload._store)), self.last_block_height+1):
                        try:
                            self.block_preload.remove(i)
                        except:
                            pass
                if self.block_preload._store_size < self.block_preload_cache_limit * 0.9:
                    continue

                await asyncio.sleep(10)
                # remove unused items

"""