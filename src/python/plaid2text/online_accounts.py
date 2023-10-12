#! /usr/bin/env python3

from collections import OrderedDict
import datetime
import os
import sys
import textwrap
import json

import plaid
from plaid.api import plaid_api
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.transactions_sync_request import TransactionsSyncRequest

import plaid2text.config_manager as cm
import plaid2text.storage_manager as storage_manager
from plaid2text.interact import prompt, clear_screen, NullValidator
from plaid2text.interact import NumberValidator, NumLengthValidator, YesNoValidator, PATH_COMPLETER


class PlaidAccess():
    def __init__(self, client_id=None, secret=None):
        if client_id and secret:
            self.client_id = client_id
            self.secret = secret
        else:
            self.client_id, self.secret = cm.get_plaid_config()

        configuration = plaid.Configuration(
            host = plaid.Environment.Development,
            api_key = {
                'clientId':self.client_id,
                'secret': self.secret,
            }
        )
        self.api_client = plaid.ApiClient(configuration)
        self.client = plaid_api.PlaidApi(self.api_client)

    def get_transactions(self,
                         access_token,
                         start_date,
                         end_date,
                         account_ids):
        """Get transaction for a given account for the given dates"""
        options = TransactionsGetRequestOptions()
        options.account_ids=[account_ids]

        request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=options
        )
        try:
            response = self.client.transactions_get(request)
        except plaid.ApiException as ex:
            response = json.loads(ex.body)
            if response['error_code'] == 'ITEM_LOGIN_REQUIRED':
                try:
                    cm.update_link_token(access_token)
                except BaseException as e:
                    if e.code == 0:
                        sys.exit(0)
                    print("Unable to update plaid account [%s] due to: " % account_ids, file=sys.stderr)
                    print("    %s" % response['error_message'], file=sys.stderr )
                    sys.exit(1)                        
            else:
                print("Unable to update plaid account [%s] due to: " % account_ids, file=sys.stderr)
                print("    %s" % response['error_message'], file=sys.stderr )
                sys.exit(1)        
        transactions = response['transactions']
        total_transactions = response['total_transactions']
        while len(transactions) < total_transactions:
            print("Fetched " + str(len(transactions)) + " of " + str(total_transactions) + " transactions...")
            options.offset = len(transactions)
            request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date(),
            end_date=end_date(),
            options=options
            )
            try:
                response = self.client.transactions_get(request)
            except plaid.ApiException as ex:
                response = json.loads(ex.body)
                print("Unable to update plaid account [%s] due to: " % account_ids, file=sys.stderr)
                print("    %s" % response['error_message'], file=sys.stderr )
                sys.exit(1)
            transactions.extend(response['transactions'])
        print("Downloaded %d transactions for %s - %s" % ( len(transactions), start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")))
        return transactions

    def sync_transactions(self, options):
        # Get all account configs
        accounts = cm.get_configured_accounts()
        configs = []
        for account in accounts:
            config = cm.get_config(account)
            config['account_name'] = account
            configs.append(config)
        # Retrieve unique access tokens (since each item has a unique access token)
        items=[]
        for config in configs:
            if 'cursor' in config:
                item = {'access_token': config['access_token'], 'cursor': config['cursor']}
                items.append(item)
            else:
                item = {'access_token': config['access_token']}
                items.append(item)
        
        items = list({tuple(sorted(d.items())): d for d in items}.values())  # deduplicating items

        newTxns = []
        for item in items:
            if len(item) == 2:
                request = TransactionsSyncRequest(
                    access_token=item['access_token'],
                    cursor=item['cursor']
                )
                startDate = None

            else:
                request = TransactionsSyncRequest(
                    access_token=item['access_token']
                )
                account_name = cm.get_account_in_item(item['access_token'])
                startDateStr = prompt('This is the first time you are syncing with the institution containing ' + account_name + ' using these credentials.\nEnter the start date for transactions you wish to download in YYYY-MM-DD format.\nLeave blank if you want to download all transactions:\n')
                startDate = datetime.datetime.strptime(startDateStr, '%Y-%m-%d').date()
            try:
                response = self.client.transactions_sync(request)
            except plaid.ApiException as ex:
                response = json.loads(ex.body)
                if response['error_code'] == 'ITEM_LOGIN_REQUIRED':
                    try:
                        cm.update_link_token(item['access_token'])
                    except BaseException as e:
                        if e.code == 0:
                            sys.exit(0)
                        print("Unable to update plaid account [%s] due to: " % account_ids, file=sys.stderr)
                        print("    %s" % response['error_message'], file=sys.stderr )
                        sys.exit(1)                        
                else:
                    print("Unable to update plaid account [%s] due to: " % account_ids, file=sys.stderr)
                    print("    %s" % response['error_message'], file=sys.stderr )
                    sys.exit(1)

            transactions = response['added']
            while (response['has_more']):
                request = TransactionsSyncRequest(
                    access_token=item['access_token'],
                    cursor=response['next_cursor']
                )
                response = self.client.transactions_sync(request)
                transactions += response['added']

        #Organize transactions by account
            uniqueAccounts = []
            for t in transactions:
                if not t['account_id'] in uniqueAccounts:
                    uniqueAccounts.append(t['account_id'])
            for a in uniqueAccounts:
                acTxns = []
                for t in transactions:
                    if t['account_id'] == a and t['pending'] == False:
                        if startDate == None or t['date'] >= startDate:
                            acTxns.append(t)
                accountIncr = SyncResponse(a, acTxns, response['next_cursor'])
                if len(accountIncr.transactions) > 0:
                    newTxns.append(accountIncr)
            item['cursor'] = response['next_cursor']
            if not startDate == None:
                pass
        store_transactions(options, newTxns)
        if len(newTxns) == 0:
            print("Checked all accounts, no new transactions")
        else:
            print("Local database synced with bank data for all accounts")
        for item in items:
            for config in configs:
                if config['access_token'] == item['access_token']:
                    cm.update_cursor(config['account_name'], item['cursor'])
        
        sys.exit(0)

def store_transactions (options, accounts):
    for account in accounts:
        if options.dbtype == 'mongodb':
            sm = storage_manager.MongoDBStorage(
                options.mongo_db,
                options.mongo_db_uri,
                account.plaid_account,
                account.posting_account
            )
        else:
            sm = storage_manager.SQLiteStorage(
                options.sqlite_db,
                account.plaid_account,
                account.posting_account
            )
        print("New transactions in "+account.plaid_account+", saving to database now")
        sm.save_transactions(account.transactions)

class SyncResponse():
    def __init__(self, account_id, acTxns, cursor):
        self.account_id = account_id
        self.transactions = acTxns
        self.plaid_account = self.plaid_account_lookup()
        self.posting_account = self.posting_account_lookup()
        self.next_cursor = cursor
    
    def plaid_account_lookup(self):
        accounts = cm.get_configured_accounts()
        for account in accounts:
            config = cm.get_config(account)
            if config['account'] == self.account_id:
                return account
                break

    def posting_account_lookup(self):
        accounts = cm.get_configured_accounts()
        for account in accounts:
            config = cm.get_config(account)
            if config['account'] == self.account_id:
                return config['posting_account']
                break