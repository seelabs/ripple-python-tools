#!/usr/local/bin/python3

#    Copyright (c) 2018 Ripple Labs Inc.
#
#    Permission to use, copy, modify, and/or distribute this software for any
#    purpose  with  or without fee is hereby granted, provided that the above
#    copyright notice and this permission notice appear in all copies.
#
#    THE  SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
#    WITH  REGARD  TO  THIS  SOFTWARE  INCLUDING  ALL  IMPLIED  WARRANTIES  OF
#    MERCHANTABILITY  AND  FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
#    ANY  SPECIAL ,  DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
#    WHATSOEVER  RESULTING  FROM  LOSS  OF USE, DATA OR PROFITS, WHETHER IN AN
#    ACTION  OF  CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
#    OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.


# Walk back through an account node's history and report all transactions
# that affect the account in reverse order (from newest to oldest).
#
# To capture results redirect stdout to a file.

import json
import re
import sys
import websocket

from websocket import create_connection

# Extract command line arguments.
def extractArgs ():

    # Default websocket connection if none provided.
    connectTo = "ws://s2.ripple.com:443"

    usage = 'usage: PyAcctThread <Account ID> [ws://<Server>:<port>]\n'\
        'If <server>:<port> are omitted defaults to "{0}"'.format (connectTo)

    # Extract the first argument: the accountID
    argCount = len (sys.argv) - 1
    if argCount < 1:
        print ('Expected account ID as first argument.\n')
        print (usage)
        sys.exit (1)  # abort because of error

    # Sanity check the accountID string
    accountId = sys.argv[1]
    idLen = len (accountId)
    if (accountId[:1] != "r") or (idLen < 25) or (idLen > 35):
        print ('Invalid format for account ID.\n'\
            'Should start with "r" with length between 25 and 35 characters.\n')
        print (usage)
        sys.exit (1)  # abort because of error

    if argCount > 2:
        print ('Too many command line arguments.\n')
        print (usage)
        sys.exit (1)  # abort because of error

    # If it's there, pick up the optional connectTo.
    if argCount == 2:
        connectTo = sys.argv[2]

    # Validate the connectTo.
    if connectTo[:5] != "ws://":
        print ('Invalid format for websocket connection.  Expected "ws://".  '\
            'Got: {0}\n'.format (connectTo))
        print (usage)
        sys.exit (1)  # abort because of error

    # Verify that the port is specified.
    if not re.search (r'\:\d+$', connectTo):
        print ('Invalid format for websocket connection.  Connection expected '\
            'to end with \nport specifier (colon followed by digits), '\
            'e.g., ws://s2.ripple.com:443')
        print ('Got: {0}\n'.format (connectTo))
        print (usage)
        sys.exit (1)  # abort because of error

    return connectTo, accountId


# Used to track websocket requests.
wsIdValue = 0

# Generate a new websocket request ID and return it.
def wsId ():
    global wsIdValue
    wsIdValue += 1
    return wsIdValue


# Get the websocket response that matches the id of the request.  All
# responses are in json, so return json.
def getResponse (ws, id):
    while True:
        msg = ws.recv ()
        jsonMsg = json.loads (msg)
        gotId = jsonMsg["id"]
        if (gotId == id):
            return jsonMsg
        print ("Unexpected websocket message id: {0}.  Expected {1}.".format (
            gotId, id))


# Request account_info, print it, and return it as JSON.
def printAccountInfo (ws, accountId):
    id = wsId ()

    cmd = """
{{
    "id" : {0},
    "command" : "account_info",
    "account" : "{1}",
    "strict" : true,
    "ledger_index" : "validated"
}}""".format (id, accountId)

    ws.send (cmd)
    jsonMsg = getResponse (ws, id)

    # Remove the websocket id to reduce noise.
    jsonMsg.pop ("id", None)
    print (json.dumps (jsonMsg, indent = 4))
    return jsonMsg


# Request tx by txnId, print it, and return the tx as JSON.
def printTx (ws, txnId):
    id = wsId ()

    cmd = """
{{
    "id" : {0},
    "command" : "tx",
    "transaction" : "{1}"
}}""".format (id, txnId)

    ws.send (cmd)
    jsonMsg = getResponse (ws, id)

    # Remove the websocket id from what we print to reduce noise.
    jsonMsg.pop ("id", None)
    print (json.dumps (jsonMsg, indent = 4))
    return jsonMsg


# Extract the PreviousTxnID for accountID given a transaction's metadata.
def getPrevTxnId (jsonMsg, accountId):

    # Handle any errors that might be in the response.
    if "error" in jsonMsg:

        # If the error is txnNotFound there there's a good chance
        # the server does not have enough history.
        if jsonMsg["error"] == "txnNotFound":
            print ("Transaction not found.  "
                "Does your server have enough history?  Unexpected stop.")
            return ""

        err = jsonMsg["error"]
        if jsonMsg[error_message]:
            err = jsonMsg[error_message]

        print (err + "  Unexpected stop.")
        return ""

    # First navigate to the metadata AffectedNodes, which should be a list.
    try:
        affectedNodes = jsonMsg["result"]["meta"]["AffectedNodes"]
    except KeyError:
        print ("No AffectedNodes found in transaction.  Unexpected stop.\n")
        print ("Got:")
        print (json.dumps (jsonMsg, indent = 4))
        return ""

    for node in affectedNodes:
        # If we find the account being created then we're successfully done.
        try:
            if node["CreatedNode"]["LedgerEntryType"] == "AccountRoot":
                if node["CreatedNode"]["NewFields"]["Account"] == accountId:
                    print (
                        'Created Account {0}.  Done.'.format (accountId))
                    return ""
        except KeyError:
            pass # If the field is not found that's okay.

        # Else look for the next transaction.
        try:
            if node["ModifiedNode"]["FinalFields"]["Account"] == accountId:
                return node["ModifiedNode"]["PreviousTxnID"]
        except KeyError:
            continue;  # If the field is not found try the next node.

    print ("No more modifying transactions found.  Unexpected stop.\n")
    print ("Got:")
    print (json.dumps (jsonMsg, indent = 4))
    return ""


# Write spinner to stderr to show liveness while walking the thread.
def SpinningCursor ():
    while True:
        for cursor in '|/-\\':
            sys.stderr.write (cursor)
            sys.stderr.flush ()
            yield
            sys.stderr.write ('\b')


# Thread the account to extract all transactions performed by that account.
#    1. Start by getting account_info for the accountId in the most recent
#       validated ledger.
#    2. account_info returns the TxId of the last transaction that affected
#       this account.
#    3. Call tx with that TxId.  Save that transaction.
#    4. The tx response should contain the AccountRoot for the accountId.
#       Extract the new value of PreviousTxID from the AccountRoot.
#    5. Return to step 3, but using the new PreviousTxID.
def threadAccount (ws, accountId):

    # Call account_info to get our starting txId.
    jsonMsg = printAccountInfo (ws, accountId)

    # Handle any errors that might be in the response.
    if "error" in jsonMsg:
        print ('No account_info for accountID {0}'.format (accountId))
        if jsonMsg["error"] == "actMalformed":
            print ("Did you mistype the accountID?")

        err = jsonMsg["error"]
        if jsonMsg["error_message"]:
            err = jsonMsg["error_message"]

        print (err + "  Unexpected stop.")
        return

    # Extract the starting txId.
    prevTxnId = ""
    try:
        prevTxnId = jsonMsg["result"]["account_data"]["PreviousTxnID"]
    except KeyError:
        print (
            "No PreviousTxnID found for {0}.  No transactions found.".format (
                accountId))
        return

    # Transaction threading loop.
    spinner = SpinningCursor ()  # Liveness indicator.
    while prevTxnId != "":
        next (spinner)
        print ("\n" + ("-" * 79) + "\n")
        jsonMsg = printTx (ws, prevTxnId)
        prevTxnId = getPrevTxnId (jsonMsg, accountId)


# Open the websocket connection and pass the websocket upstream for use.
def openConnection (connectTo, accountId):
    try:
        ws = create_connection (connectTo)
    except websocket._exceptions.WebSocketAddressException:
        print ("Unable to open connection to {0}.\n".format (connectTo))
        return;
    except ConnectionRefusedError:
        print ("Connection to {0} refused.\n".format (connectTo))
        return

    try:
        threadAccount (ws, accountId)
    finally:
        ws.close ()
        sys.stderr.write ('\b') # Clean up from SpinningCursor


if __name__ == "__main__":
    # Get command line arguments.
    connectTo, accountId = extractArgs ()

    # Open the connection then thread the account.
    openConnection (connectTo, accountId)
