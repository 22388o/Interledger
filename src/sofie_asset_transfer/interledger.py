from .interfaces import Initiator, Responder
from .state_interfaces import StateInitiator, StateResponder, AssetState
import time, asyncio
from asyncio import Condition
from typing import List
import concurrent.futures
from enum import Enum
from db_manager.db_manager import DBManager

class State(Enum):
    """State of a transfer during the protocol
    """
    READY = 1
    SENT = 2
    COMPLETED = 3
    PROCESSED = 4


class Transfer(object):
    """The information paired to a data transfer: its 'future' async call to accept(); the 'result' of the accept();
    the transfer 'state'; the event transfer 'data'.
    """

    def __init__(self):
        self.future = None
        self.result = None
        self.state = State.READY
        self.data = None        # transactional data bundle


class Interledger(object):
    """
    Class implementing the interledger component. The component is composed by an Initiator and a Responder to implement the asset transfer operation.
    """

    def __init__(self, initiator: Initiator, responder: Responder):
        """Constructor
        :param object initiator: The Initiator object
        :param object responder: The Responder object
        """
        self.initiator = initiator
        self.responder = responder
        self.transfers = []
        self.transfers_sent = []
        self.pending = 0
        self.keep_running = True

        # Debug
        self.results_abort = []
        self.results_commit = []


    def stop(self):
        """Stop the interledger run() operation
        """
        self.keep_running = False

        
    # blocking, trigger
    async def receive_transfer(self):
        """Receive the list of transfers from the Initiator. This operation blocks until it receives at least one transfer.
        """
        # The user has already called transferOut()
        # State of the asset:
            # (Here | Not Here) -> (TransferOut | Not Here)
        transfers = await self.initiator.get_transfers()
        if len(transfers) > 0:
            self.transfers.extend(transfers)
    
        return len(transfers)

 
    # non blocking, action
    async def send_transfer(self):
        """Forward the stored transfers to the Responder.
        """

        for t in self.transfers:
            if t.state == State.READY:
                t.state = State.SENT
                # call accept()
                # State of the asset:
                    # (TransferOut | Not Here) -> (TransferOut | Here)
                t.future = asyncio.ensure_future(self.responder.receive_transfer(t))
                self.transfers_sent.append(t)
                self.pending += 1


    # blocking, trigger
    async def transfer_result(self):
        """Store the results of the transfers sent to the Responder. This operation blocks until at least one transfer has been completed.
        """

        futures = [t.future for t in self.transfers_sent]
        await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED)

        for t in self.transfers_sent:
            if t.state == State.SENT and not t.state == State.COMPLETED and t.future.done():
                t.result = t.future.result()
                t.state = State.COMPLETED


    # non blocking: action
    async def process_result(self):
        """Process the result of the responder: trigger the commit() or the abort() operation of the Initiator to complete the protocol.
        """

        for t in self.transfers_sent:

            if t.state == State.COMPLETED:
                
                if t.result:
                    # accept() goes well
                    # State of the asset:
                        # (TransferOut | Here) -> (Not Here | Here)
                    asyncio.ensure_future(self.initiator.commit_transfer(t))
                    self.results_commit.append(t.result)
                else:
                    # Error during accept() from Responder
                        # accept() should not have changed the state of the Asset
                    # State of the asset:
                        # (TransferOut | Not Here) -> (Here | Not Here)
                    asyncio.ensure_future(self.initiator.abort_transfer(t))
                    self.results_abort.append(t.result)

                t.state = State.PROCESSED
                self.pending -= 1


    async def run(self):
        """Run the interledger.
        Wait for new transfers from the Initiator, forward them to the Responder and finalize the protocol with the Intiator.
        """
 
        while self.keep_running:
            
            receive = asyncio.ensure_future(self.receive_transfer())

            if not self.pending:
                await receive
            else:
                result = asyncio.ensure_future(self.transfer_result())
                await asyncio.wait([receive, result], return_when=asyncio.FIRST_COMPLETED)

            send = asyncio.ensure_future(self.send_transfer())
            process = asyncio.ensure_future(self.process_result())

            await send
            await process


        # TODO Add some closing procedure


class StateInterledger(Interledger):
    """
    Subclass of Interledger supporting StateInitiator and StateResponder
    """

    def __init__(self, initiator: StateInitiator, responder: StateResponder):
        """Get pending transfers and put them in the lists used in the protocol
        """
        super().__init__(initiator, responder)
        self.transfers = self.restore_pending_transfers_ready()
        self.transfers_sent = self.restore_pending_transfers_sent()
        self.pending += len(self.transfers_sent)


    def restore_pending_transfers_ready(self):
        """Query the current transfers in state (TransferOut | NotHere)
        from Initiator: TransferOut
        from Responder: NotHere
        The protocol has to accept and then commit such transfers 
        """
        t_list_initiator = self.initiator.query_by_state(AssetState.TRANSFER_OUT)
        t_list_responder = self.responder.query_by_state(AssetState.NOT_HERE)
        ids = list(set(t_list_initiator).intersection(t_list_responder))

        new_transfers = []
        for i in ids:
            t = Transfer()
            t.data = {}
            t.data["assetId"] = i
            new_transfers.append(t)
        # new_transfers = [Transfer() for i in ids]
        return new_transfers


    def restore_pending_transfers_sent(self):
        """Query the current transfers in state (TransferOut | Here)
        from Initiator: TransferOut
        from Responder: Here
        The protocol has already accepted and needs to commit such transfers 
        """
        t_list_initiator = self.initiator.query_by_state(AssetState.TRANSFER_OUT)
        t_list_responder = self.responder.query_by_state(AssetState.HERE)
        ids = list(set(t_list_initiator).intersection(t_list_responder))
        
        new_transfers = []
        for i in ids:
            t = Transfer()
            t.data = {}
            t.data["assetId"] = i
            t.state = State.COMPLETED # accept() already been called by Responder
            t.result = True # needs to commit() this uncomplted transfer
            new_transfers.append(t)
        # new_transfers = [Transfer() for i in ids]
        return new_transfers