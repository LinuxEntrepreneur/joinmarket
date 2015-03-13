#from joinmarket import *
import subprocess
import unittest
import json, threading, abc, pprint, time, random
from decimal import Decimal
import bitcoin as btc

import common

def get_blockchain_interface_instance(config):
	source = config.get("BLOCKCHAIN", "blockchain_source")
	bitcoin_cli_cmd = config.get("BLOCKCHAIN", "bitcoin_cli_cmd").split(' ')
	testnet = common.get_network()=='testnet'
	if source == 'json-rpc':
		bc_interface = BitcoinCoreInterface(bitcoin_cli_cmd, testnet)
	elif source == 'regtest':
		bc_interface = RegtestBitcoinCoreInterface(bitcoin_cli_cmd)
	elif source == 'blockr':
		bc_interface = BlockrInterface(testnet)
	else:
		raise ValueError("Invalid blockchain source")	
	return bc_interface


#download_wallet_history() find_unspent_addresses() #finding where to put index and my utxos
#add address notify()
#fetchtx() needs to accept a list of addresses too
#pushtx()
class BlockchainInterface(object):
	__metaclass__ = abc.ABCMeta
	def __init__(self):
		pass

	@abc.abstractmethod
	def sync_wallet(self, wallet, gaplimit=6):
		'''Finds used addresses and utxos, puts in wallet.index and wallet.unspent'''
		pass

	@abc.abstractmethod
	def add_tx_notify(self, tx, unconfirmfun, confirmfun):
		'''Invokes unconfirmfun and confirmfun when tx is seen on the network'''
		pass

	@abc.abstractmethod
	def fetchtx(self, txid):
		'''Returns a txhash of a given txid, or list of txids'''
		pass

	@abc.abstractmethod
	def pushtx(self, txhash):
		'''pushes tx to the network, returns txhash'''
		pass

class BlockrInterface(BlockchainInterface):
	def __init__(self, testnet = False):
		super(BlockrInterface, self).__init__()
		self.network = 'testnet' if testnet else 'btc' #see bci.py in bitcoin module
		self.blockr_domain = 'tbtc' if testnet else 'btc'
    
	def sync_wallet(self, wallet, gaplimit=6):
		common.debug('downloading wallet history')
		#sets Wallet internal indexes to be at the next unused address
		addr_req_count = 20
		for mix_depth in range(wallet.max_mix_depth):
			for forchange in [0, 1]:
				unused_addr_count = 0
				last_used_addr = ''
				while unused_addr_count < gaplimit:
					addrs = [wallet.get_new_addr(mix_depth, forchange) for i in range(addr_req_count)]

					#TODO send a pull request to pybitcointools
					# because this surely should be possible with a function from it
					blockr_url = 'http://' + self.blockr_domain + '.blockr.io/api/v1/address/txs/'
					res = btc.make_request(blockr_url+','.join(addrs))
					data = json.loads(res)['data']
					for dat in data:
						if dat['nb_txs'] != 0:
							last_used_addr = dat['address']
						else:
							unused_addr_count += 1
							if unused_addr_count >= gaplimit:
								break
				if last_used_addr == '':
					wallet.index[mix_depth][forchange] = 0
				else:
					wallet.index[mix_depth][forchange] = wallet.addr_cache[last_used_addr][2] + 1

		#finds utxos in the wallet

		addrs = {}
		for m in range(wallet.max_mix_depth):
			for forchange in [0, 1]:
				for n in range(wallet.index[m][forchange]):
					addrs[wallet.get_addr(m, forchange, n)] = m
		if len(addrs) == 0:
			common.debug('no tx used')
			return

		#TODO handle the case where there are so many addresses it cant
		# fit into one api call (>50 or so)
		i = 0
		addrkeys = addrs.keys()
		while i < len(addrkeys):
			inc = min(len(addrkeys) - i, addr_req_count)
			req = addrkeys[i:i + inc]
			i += inc

			#TODO send a pull request to pybitcointools 
			# unspent() doesnt tell you which address, you get a bunch of utxos
			# but dont know which privkey to sign with
			
			blockr_url = 'http://' + self.blockr_domain + '.blockr.io/api/v1/address/unspent/'
			res = btc.make_request(blockr_url+','.join(req))
			data = json.loads(res)['data']
			if 'unspent' in data:
				data = [data]
			for dat in data:
				for u in dat['unspent']:
					wallet.unspent[u['tx']+':'+str(u['n'])] = {'address':
						dat['address'], 'value': int(u['amount'].replace('.', ''))}

	def add_tx_notify(self, txd, unconfirmfun, confirmfun):
		unconfirm_timeout = 5*60 #seconds
		unconfirm_poll_period = 5
		confirm_timeout = 120*60
		confirm_poll_period = 5*60
		class NotifyThread(threading.Thread):
			def __init__(self, blockr_domain, txd, unconfirmfun, confirmfun):
				threading.Thread.__init__(self)
				self.daemon = True
				self.blockr_domain = blockr_domain
				self.unconfirmfun = unconfirmfun
				self.confirmfun = confirmfun
				self.tx_output_set = set([(sv['script'], sv['value']) for sv in txd['outs']])
				self.output_addresses = [btc.script_to_address(scrval[0],
					common.get_addr_vbyte()) for scrval in self.tx_output_set]
				common.debug('txoutset=' + pprint.pformat(self.tx_output_set))
				common.debug('outaddrs=' + ','.join(self.output_addresses))

			def run(self):
				st = int(time.time())
				unconfirmed_txid = None
				unconfirmed_txhex = None
				while not unconfirmed_txid:
					time.sleep(unconfirm_poll_period)
					if int(time.time()) - st > unconfirm_timeout:
						debug('checking for unconfirmed tx timed out')
						return
					blockr_url = 'http://' + self.blockr_domain + '.blockr.io/api/v1/address/unspent/'
					random.shuffle(self.output_addresses) #seriously weird bug with blockr.io
					data = json.loads(btc.make_request(blockr_url + ','.join(self.output_addresses) + '?unconfirmed=1'))['data']
					shared_txid = None
					for unspent_list in data:
						txs = set([str(txdata['tx']) for txdata in unspent_list['unspent']])
						if not shared_txid:
							shared_txid = txs
						else:	
							shared_txid = shared_txid.intersection(txs)
					common.debug('sharedtxid = ' + str(shared_txid))
					if len(shared_txid) == 0:
						continue
					blockr_url = 'http://' + self.blockr_domain + '.blockr.io/api/v1/tx/raw/'
					data = json.loads(btc.make_request(blockr_url + ','.join(shared_txid)))['data']
					if not isinstance(data, list):
						data = [data]
					for txinfo in data:
						outs = set([(sv['script'], sv['value']) for sv in btc.deserialize(txinfo['tx']['hex'])['outs']])
						print 'outs = ' + str(outs)
						if outs == self.tx_output_set:
							unconfirmed_txid = txinfo['tx']['txid']
							unconfirmed_txhex = txinfo['tx']['hex']
							break

				self.unconfirmfun(btc.deserialize(unconfirmed_txhex), unconfirmed_txid)

				st = int(time.time())
				confirmed_txid = None
				confirmed_txhex = None
				while not confirmed_txid:
					time.sleep(confirm_poll_period)
					if int(time.time()) - st > confirm_timeout:
						debug('checking for confirmed tx timed out')
						return
					blockr_url = 'http://' + self.blockr_domain + '.blockr.io/api/v1/address/txs/'
					data = json.loads(btc.make_request(blockr_url + ','.join(self.output_addresses)))['data']
					shared_txid = None
					for addrtxs in data:
						txs = set([str(txdata['tx']) for txdata in addrtxs['txs']])
						if not shared_txid:
							shared_txid = txs
						else:	
							shared_txid = shared_txid.intersection(txs)
					common.debug('sharedtxid = ' + str(shared_txid))
					if len(shared_txid) == 0:
						continue
					blockr_url = 'http://' + self.blockr_domain + '.blockr.io/api/v1/tx/raw/'
					data = json.loads(btc.make_request(blockr_url + ','.join(shared_txid)))['data']
					if not isinstance(data, list):
						data = [data]
					for txinfo in data:
						outs = set([(sv['script'], sv['value']) for sv in btc.deserialize(txinfo['tx']['hex'])['outs']])
						print 'outs = ' + str(outs)
						if outs == self.tx_output_set:
							confirmed_txid = txinfo['tx']['txid']
							confirmed_txhex = txinfo['tx']['hex']
							break
				self.confirmfun(btc.deserialize(confirmed_txhex), confirmed_txid, 1)

		NotifyThread(self.blockr_domain, txd, unconfirmfun, confirmfun).start()

	def fetchtx(self, txid):
		return btc.blockr_fetchtx(txid, self.network)

	def pushtx(self, txhex):
		data = json.loads(btc.blockr_pushtx(txhex, self.network))
		if data['status'] != 'success':
			#error message generally useless so there might not be a point returning
			debug(data) 
			return None
		return data['data']
		

class BitcoinCoreInterface(BlockchainInterface):
    def __init__(self, bitcoin_cli_cmd, testnet=False):
	super(BitcoinCoreInterface, self).__init__()
        #self.command_params = ['bitcoin-cli', '-port='+str(port), '-rpcport='+str(rpcport),'-testnet']
        self.command_params = bitcoin_cli_cmd
	if testnet:
		self.command_params += ['-testnet']
        #quick check that it's up else quit
        try:
            res = self.rpc(['getbalance'])
        except Exception as e:
            print e
    
    def get_net_info(self):
        print 'not yet done'
        
    def rpc(self, args, accept_failure=[]):
        try:
            #print 'making an rpc call with these parameters: '
            common.debug(str(self.command_params+args))
            res = subprocess.check_output(self.command_params+args)
        except subprocess.CalledProcessError, e:
            if e.returncode in accept_failure:
                return ''
            raise
        return res

    def send_tx(self, tx_hexs, query_params):
	'''csv params contains only tx hex'''
	for txhex in tx_hexs:
	    res = self.rpc(['sendrawtransaction', txhex])
        #TODO only handles a single push; handle multiple
        return {'data':res}

    def get_utxos_from_addr(self, addresses, query_params):
        r = []
        for address in addresses:
            res = json.loads(self.rpc(['listunspent','1','9999999','[\"'+address+'\"]']))
            unspents=[]
            for u in res:
                unspents.append({'tx':u['txid'],'n':u['vout'],'amount':str(u['amount']),'address':address,'confirmations':u['confirmations']})
            r.append({'address':address,'unspent':unspents})
        return {'data':r}
    
    def get_txs_from_addr(self, addresses, query_params):
        #use listtransactions and then filter
        #e.g.: -regtest listtransactions 'watchonly' 1000 0 true
        #to get the last 1000 transactions TODO 1000 is arbitrary
        acct_addrlist = self.rpc(['getaddressesbyaccount', 'watchonly'])
        for address in addresses:
            if address not in acct_addrlist:
                self.rpc(['importaddress', address, 'watchonly'],[4])            
        res = json.loads(self.rpc(['listtransactions','watchonly','2000','0','true']))
        
        result=[]
        for address in addresses:
            nbtxs = 0
            txs=[]
            for a in res:
                if a['address'] != address:
                    continue
                nbtxs += 1
                txs.append({'confirmations':a['confirmations'],'tx':a['txid'],'amount':a['amount']})
            result.append({'nb_txs':nbtxs,'address':address,'txs':txs})
        return {'data':result} 
    
    def get_tx_info(self, txhashes, query_params):
	'''Returns a list of vouts if first entry in query params is False, else returns tx hex'''
	#TODO: handle more than one tx hash
        res = json.loads(self.rpc(['getrawtransaction', txhashes[0], '1']))
        if not query_params[0]:
            return {'data':{'tx':{'hex':res['hex']}}}
        tx = btc.deserialize(res['hex'])
        #build vout list
        vouts = []
        n=0
        for o in tx['outs']:
            vouts.append({'n':n,'amount':o['value'],'address':btc.script_to_address(o['script'],0x6f)})
            n+=1
        
        return {'data':{'vouts':vouts}}
   
    def get_balance_at_addr(self, addresses, query_params):
        #NB This will NOT return coinbase coins (but wont matter in our use case).
        #In order to have the Bitcoin RPC read balances at addresses
        #it doesn't own, we must import the addresses as watch-only 
        #Note that this is a 0.10 feature; won't work with older bitcoin clients.
        #TODO : there can be a performance issue with rescanning here.
	#TODO: This code is WRONG, reports *received* coins in total, not current balance.
        #allow importaddress to fail in case the address is already in the wallet
        res = []
        for address in addresses:
            self.rpc(['importaddress', address,'watchonly'],[4])
            res.append({'address':address,'balance':\
                        int(Decimal(1e8) * Decimal(self.rpc(['getreceivedbyaddress', address])))})
        return {'data':res}

    #Not used; I think, not needed
    '''def get_addr_from_utxo(self, txhash, index):
        #get the transaction details
        res = json.loads(self.rpc(['gettxout', txhash, str(index)]))
        amt = int(Decimal(1e8)*Decimal(res['value']))
        address = res('addresses')[0]
        return (address, amt)
        '''
    
#class for regtest chain access
#running on local daemon. Only 
#to be instantiated after network is up
#with > 100 blocks.
class RegtestBitcoinCoreInterface(BitcoinCoreInterface):
    def __init__(self, bitcoin_cli_cmd):
	super(BitcoinCoreInterface, self).__init__()
        #self.command_params = ['bitcoin-cli', '-port='+str(port), '-rpcport='+str(rpcport),'-testnet']
        self.command_params = bitcoin_cli_cmd + ['-regtest']
        #quick check that it's up else quit
        try:
            res = self.rpc(['getbalance'])
        except Exception as e:
            print e
    
    def send_tx(self, tx_hex, query_params):
        super(RegtestBitcoinCoreInterface, self).send_tx(tx_hex, query_params)
        self.tick_forward_chain(1)
        
    def tick_forward_chain(self, n):
            '''Special method for regtest only;
            instruct to mine n blocks.'''
            self.rpc(['setgenerate','true', str(n)])
    
    def grab_coins(self, receiving_addr, amt=50):
        '''
        NOTE! amt is passed in Coins, not Satoshis!
        Special method for regtest only:
        take coins from bitcoind's own wallet
        and put them in the receiving addr.
        Return the txid.
        '''
        if amt > 500:
            raise Exception("too greedy")
	'''
        if amt > self.current_balance:
            #mine enough to get to the reqd amt
            reqd = int(amt - self.current_balance)
            reqd_blocks = str(int(reqd/50) +1)
            if self.rpc(['setgenerate','true', reqd_blocks]):
                raise Exception("Something went wrong")
	'''
        #now we do a custom create transaction and push to the receiver
        txid = self.rpc(['sendtoaddress', receiving_addr, str(amt)])
        if not txid:
            raise Exception("Failed to broadcast transaction")
        #confirm
        self.tick_forward_chain(1)
        return txid        

def main():
    myBCI = RegtestBitcoinCoreInterface()
    #myBCI.send_tx('stuff')
    print myBCI.get_utxos_from_addr(["n4EjHhGVS4Rod8ociyviR3FH442XYMWweD"])
    print myBCI.get_balance_at_addr(["n4EjHhGVS4Rod8ociyviR3FH442XYMWweD"])
    txid = myBCI.grab_coins('mygp9fsgEJ5U7jkPpDjX9nxRj8b5nC3Hnd',23)
    print txid
    print myBCI.get_balance_at_addr(['mygp9fsgEJ5U7jkPpDjX9nxRj8b5nC3Hnd'])
    print myBCI.get_utxos_from_addr(['mygp9fsgEJ5U7jkPpDjX9nxRj8b5nC3Hnd'])

if __name__ == '__main__':
    main()




