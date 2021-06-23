import time
from typing import Callable, Optional, Any, Dict

from blspy import AugSchemeMPL, G2Element, G1Element

import chia.server.ws_connection as ws
from chia.consensus.pot_iterations import calculate_iterations_quality, calculate_sp_interval_iters
from chia.farmer.farmer import Farmer
from chia.farmer.pooling.og_pool_protocol import PartialPayload, SubmitPartial
from chia.protocols import farmer_protocol, harvester_protocol
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.server.outbound_message import NodeType, make_msg
from chia.types.blockchain_format.pool_target import PoolTarget
from chia.types.blockchain_format.proof_of_space import ProofOfSpace
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.api_decorators import api_request, peer_required
from chia.util.ints import uint32, uint64


class FarmerAPI:
    farmer: Farmer

    def __init__(self, farmer) -> None:
        self.farmer = farmer

    def _set_state_changed_callback(self, callback: Callable):
        self.farmer.state_changed_callback = callback

    @api_request
    @peer_required
    async def new_proof_of_space(
        self, new_proof_of_space: harvester_protocol.NewProofOfSpace, peer: ws.WSChiaConnection
    ):
        """
        This is a response from the harvester, for a NewChallenge. Here we check if the proof
        of space is sufficiently good, and if so, we ask for the whole proof.
        """
        if new_proof_of_space.sp_hash not in self.farmer.number_of_responses:
            self.farmer.number_of_responses[new_proof_of_space.sp_hash] = 0
            self.farmer.cache_add_time[new_proof_of_space.sp_hash] = uint64(int(time.time()))

        max_pos_per_sp = 5
        if self.farmer.number_of_responses[new_proof_of_space.sp_hash] > max_pos_per_sp:
            # This will likely never happen for any farmer with less than 10% of global space
            # It's meant to make testnets more stable
            self.farmer.log.info(
                f"Surpassed {max_pos_per_sp} PoSpace for one SP, no longer submitting PoSpace for signage point "
                f"{new_proof_of_space.sp_hash}"
            )
            return None

        if new_proof_of_space.sp_hash not in self.farmer.sps:
            self.farmer.log.warning(
                f"Received response for a signage point that we do not have {new_proof_of_space.sp_hash}"
            )
            return None

        sps = self.farmer.sps[new_proof_of_space.sp_hash]
        for sp in sps:
            computed_quality_string = new_proof_of_space.proof.verify_and_get_quality_string(
                self.farmer.constants,
                new_proof_of_space.challenge_hash,
                new_proof_of_space.sp_hash,
            )
            if computed_quality_string is None:
                self.farmer.log.error(f"Invalid proof of space {new_proof_of_space.proof}")
                return None

            self.farmer.number_of_responses[new_proof_of_space.sp_hash] += 1

            required_iters: uint64 = calculate_iterations_quality(
                self.farmer.constants.DIFFICULTY_CONSTANT_FACTOR,
                computed_quality_string,
                new_proof_of_space.proof.size,
                sp.difficulty,
                new_proof_of_space.sp_hash,
            )
            # If the iters are good enough to make a block, proceed with the block making flow
            if required_iters < calculate_sp_interval_iters(self.farmer.constants, sp.sub_slot_iters):
                # Proceed at getting the signatures for this PoSpace
                request = harvester_protocol.RequestSignatures(
                    new_proof_of_space.plot_identifier,
                    new_proof_of_space.challenge_hash,
                    new_proof_of_space.sp_hash,
                    [sp.challenge_chain_sp, sp.reward_chain_sp],
                )

                if new_proof_of_space.sp_hash not in self.farmer.proofs_of_space:
                    self.farmer.proofs_of_space[new_proof_of_space.sp_hash] = [
                        (
                            new_proof_of_space.plot_identifier,
                            new_proof_of_space.proof,
                        )
                    ]
                else:
                    self.farmer.proofs_of_space[new_proof_of_space.sp_hash].append(
                        (
                            new_proof_of_space.plot_identifier,
                            new_proof_of_space.proof,
                        )
                    )
                self.farmer.cache_add_time[new_proof_of_space.sp_hash] = uint64(int(time.time()))
                self.farmer.quality_str_to_identifiers[computed_quality_string] = (
                    new_proof_of_space.plot_identifier,
                    new_proof_of_space.challenge_hash,
                    new_proof_of_space.sp_hash,
                    peer.peer_node_id,
                )
                self.farmer.cache_add_time[computed_quality_string] = uint64(int(time.time()))

                return make_msg(ProtocolMessageTypes.request_signatures, request)

            pool_public_key = new_proof_of_space.proof.pool_public_key
            if pool_public_key is not None and self.farmer.is_pooling_enabled():
                await self.process_new_proof_of_space_for_pool(
                    new_proof_of_space,
                    peer,
                    pool_public_key,
                    computed_quality_string
                )

            return

    @api_request
    async def respond_signatures(self, response: harvester_protocol.RespondSignatures):
        """
        There are two cases: receiving signatures for sps, or receiving signatures for the block.
        """
        if response.sp_hash not in self.farmer.sps:
            self.farmer.log.warning(f"Do not have challenge hash {response.challenge_hash}")
            return None
        is_sp_signatures: bool = False
        sps = self.farmer.sps[response.sp_hash]
        signage_point_index = sps[0].signage_point_index
        found_sp_hash_debug = False
        for sp_candidate in sps:
            if response.sp_hash == response.message_signatures[0][0]:
                found_sp_hash_debug = True
                if sp_candidate.reward_chain_sp == response.message_signatures[1][0]:
                    is_sp_signatures = True
        if found_sp_hash_debug:
            assert is_sp_signatures

        pospace = None
        for plot_identifier, candidate_pospace in self.farmer.proofs_of_space[response.sp_hash]:
            if plot_identifier == response.plot_identifier:
                pospace = candidate_pospace
        assert pospace is not None

        computed_quality_string = pospace.verify_and_get_quality_string(
            self.farmer.constants, response.challenge_hash, response.sp_hash
        )
        if computed_quality_string is None:
            self.farmer.log.warning(f"Have invalid PoSpace {pospace}")
            return None

        if is_sp_signatures:
            (
                challenge_chain_sp,
                challenge_chain_sp_harv_sig,
            ) = response.message_signatures[0]
            reward_chain_sp, reward_chain_sp_harv_sig = response.message_signatures[1]
            for sk in self.farmer.get_private_keys():
                pk = sk.get_g1()
                if pk == response.farmer_pk:
                    agg_pk = ProofOfSpace.generate_plot_public_key(response.local_pk, pk)
                    assert agg_pk == pospace.plot_public_key
                    farmer_share_cc_sp = AugSchemeMPL.sign(sk, challenge_chain_sp, agg_pk)
                    agg_sig_cc_sp = AugSchemeMPL.aggregate([challenge_chain_sp_harv_sig, farmer_share_cc_sp])
                    assert AugSchemeMPL.verify(agg_pk, challenge_chain_sp, agg_sig_cc_sp)

                    # This means it passes the sp filter
                    farmer_share_rc_sp = AugSchemeMPL.sign(sk, reward_chain_sp, agg_pk)
                    agg_sig_rc_sp = AugSchemeMPL.aggregate([reward_chain_sp_harv_sig, farmer_share_rc_sp])
                    assert AugSchemeMPL.verify(agg_pk, reward_chain_sp, agg_sig_rc_sp)

                    if pospace.pool_public_key is not None:
                        assert pospace.pool_contract_puzzle_hash is None
                        pool_pk = bytes(pospace.pool_public_key)
                        if pool_pk not in self.farmer.pool_sks_map:
                            self.farmer.log.error(
                                f"Don't have the private key for the pool key used by harvester: {pool_pk.hex()}"
                            )
                            return None

                        pool_target: Optional[PoolTarget] = PoolTarget(self.farmer.pool_target, uint32(0))
                        assert pool_target is not None
                        pool_target_signature: Optional[G2Element] = AugSchemeMPL.sign(
                            self.farmer.pool_sks_map[pool_pk], bytes(pool_target)
                        )
                    else:
                        assert pospace.pool_contract_puzzle_hash is not None
                        pool_target = None
                        pool_target_signature = None

                    request = farmer_protocol.DeclareProofOfSpace(
                        response.challenge_hash,
                        challenge_chain_sp,
                        signage_point_index,
                        reward_chain_sp,
                        pospace,
                        agg_sig_cc_sp,
                        agg_sig_rc_sp,
                        self.farmer.farmer_target,
                        pool_target,
                        pool_target_signature,
                    )
                    self.farmer.state_changed("proof", {"proof": request, "passed_filter": True})
                    msg = make_msg(ProtocolMessageTypes.declare_proof_of_space, request)
                    await self.farmer.server.send_to_all([msg], NodeType.FULL_NODE)
                    return None

        else:
            # This is a response with block signatures
            for sk in self.farmer.get_private_keys():
                (
                    foliage_block_data_hash,
                    foliage_sig_harvester,
                ) = response.message_signatures[0]
                (
                    foliage_transaction_block_hash,
                    foliage_transaction_block_sig_harvester,
                ) = response.message_signatures[1]
                pk = sk.get_g1()
                if pk == response.farmer_pk:
                    agg_pk = ProofOfSpace.generate_plot_public_key(response.local_pk, pk)
                    assert agg_pk == pospace.plot_public_key
                    foliage_sig_farmer = AugSchemeMPL.sign(sk, foliage_block_data_hash, agg_pk)
                    foliage_transaction_block_sig_farmer = AugSchemeMPL.sign(sk, foliage_transaction_block_hash, agg_pk)
                    foliage_agg_sig = AugSchemeMPL.aggregate([foliage_sig_harvester, foliage_sig_farmer])
                    foliage_block_agg_sig = AugSchemeMPL.aggregate(
                        [foliage_transaction_block_sig_harvester, foliage_transaction_block_sig_farmer]
                    )
                    assert AugSchemeMPL.verify(agg_pk, foliage_block_data_hash, foliage_agg_sig)
                    assert AugSchemeMPL.verify(agg_pk, foliage_transaction_block_hash, foliage_block_agg_sig)

                    request_to_nodes = farmer_protocol.SignedValues(
                        computed_quality_string,
                        foliage_agg_sig,
                        foliage_block_agg_sig,
                    )

                    msg = make_msg(ProtocolMessageTypes.signed_values, request_to_nodes)
                    await self.farmer.server.send_to_all([msg], NodeType.FULL_NODE)

    """
    FARMER PROTOCOL (FARMER <-> FULL NODE)
    """

    @api_request
    async def new_signage_point(self, new_signage_point: farmer_protocol.NewSignagePoint):
        difficulty = new_signage_point.difficulty
        sub_slot_iters = new_signage_point.sub_slot_iters
        if self.farmer.is_pooling_enabled():
            difficulty = self.farmer.pool_difficulty
            sub_slot_iters = self.farmer.pool_sub_slot_iters

        message = harvester_protocol.NewSignagePointHarvester(
            new_signage_point.challenge_hash,
            difficulty,
            sub_slot_iters,
            new_signage_point.signage_point_index,
            new_signage_point.challenge_chain_sp,
        )

        msg = make_msg(ProtocolMessageTypes.new_signage_point_harvester, message)
        await self.farmer.server.send_to_all([msg], NodeType.HARVESTER)
        if new_signage_point.challenge_chain_sp not in self.farmer.sps:
            self.farmer.sps[new_signage_point.challenge_chain_sp] = []
        if new_signage_point in self.farmer.sps[new_signage_point.challenge_chain_sp]:
            self.farmer.log.debug(f"Duplicate signage point {new_signage_point.signage_point_index}")
            return

        self.farmer.sps[new_signage_point.challenge_chain_sp].append(new_signage_point)
        self.farmer.cache_add_time[new_signage_point.challenge_chain_sp] = uint64(int(time.time()))
        self.farmer.state_changed("new_signage_point", {"sp_hash": new_signage_point.challenge_chain_sp})

    @api_request
    async def request_signed_values(self, full_node_request: farmer_protocol.RequestSignedValues):
        if full_node_request.quality_string not in self.farmer.quality_str_to_identifiers:
            self.farmer.log.error(f"Do not have quality string {full_node_request.quality_string}")
            return None

        (plot_identifier, challenge_hash, sp_hash, node_id) = self.farmer.quality_str_to_identifiers[
            full_node_request.quality_string
        ]
        request = harvester_protocol.RequestSignatures(
            plot_identifier,
            challenge_hash,
            sp_hash,
            [full_node_request.foliage_block_data_hash, full_node_request.foliage_transaction_block_hash],
        )

        msg = make_msg(ProtocolMessageTypes.request_signatures, request)
        await self.farmer.server.send_to_specific([msg], node_id)

    @api_request
    async def farming_info(self, request: farmer_protocol.FarmingInfo):
        self.farmer.state_changed(
            "new_farming_info",
            {
                "farming_info": {
                    "challenge_hash": request.challenge_hash,
                    "signage_point": request.sp_hash,
                    "passed_filter": request.passed,
                    "proofs": request.proofs,
                    "total_plots": request.total_plots,
                    "timestamp": request.timestamp,
                }
            },
        )

    async def process_new_proof_of_space_for_pool(
            self,
            new_proof_of_space: harvester_protocol.NewProofOfSpace,
            peer: ws.WSChiaConnection,
            pool_public_key: G1Element,
            computed_quality_string: bytes32
    ):
        # Otherwise, send the proof of space to the pool
        # When we win a block, we also send the partial to the pool
        required_iters = calculate_iterations_quality(
            self.farmer.constants.DIFFICULTY_CONSTANT_FACTOR,
            computed_quality_string,
            new_proof_of_space.proof.size,
            self.farmer.pool_difficulty,
            new_proof_of_space.sp_hash,
        )
        if required_iters >= self.farmer.iters_limit:
            self.farmer.log.info(
                f"Proof of space not good enough for pool difficulty of {self.farmer.pool_difficulty}"
            )
            return

        # Submit partial to pool
        is_eos = new_proof_of_space.signage_point_index == 0
        payload = PartialPayload(
            new_proof_of_space.proof,
            self.farmer.pool_difficulty,
            new_proof_of_space.sp_hash,
            is_eos,
            self.farmer.pool_payout_address
        )

        # The plot key is 2/2 so we need the harvester's half of the signature
        m_to_sign = payload.get_hash()
        request = harvester_protocol.RequestSignatures(
            new_proof_of_space.plot_identifier,
            new_proof_of_space.challenge_hash,
            new_proof_of_space.sp_hash,
            [m_to_sign],
        )
        response: Any = await peer.request_signatures(request)
        if not isinstance(response, harvester_protocol.RespondSignatures):
            self.farmer.log.error(f"Invalid response from harvester: {response}")
            return

        assert len(response.message_signatures) == 1

        plot_signature: Optional[G2Element] = None
        for sk in self.farmer.get_private_keys():
            pk = sk.get_g1()
            if pk == response.farmer_pk:
                agg_pk = ProofOfSpace.generate_plot_public_key(response.local_pk, pk)
                assert agg_pk == new_proof_of_space.proof.plot_public_key
                sig_farmer = AugSchemeMPL.sign(sk, m_to_sign, agg_pk)
                plot_signature = AugSchemeMPL.aggregate([sig_farmer, response.message_signatures[0][1]])
                assert AugSchemeMPL.verify(agg_pk, m_to_sign, plot_signature)
        pool_sk = self.farmer.pool_sks_map[bytes(pool_public_key)]
        authentication_signature = AugSchemeMPL.sign(pool_sk, m_to_sign)

        assert plot_signature is not None
        agg_sig: G2Element = AugSchemeMPL.aggregate([plot_signature, authentication_signature])

        submit_partial = SubmitPartial(payload, agg_sig)
        self.farmer.log.debug("Submitting partial ..")
        self.farmer.last_pool_partial_submit_timestamp = time.time()
        submit_partial_response: Dict
        try:
            submit_partial_response = await self.farmer.pool_api_client.submit_partial(submit_partial)
        except Exception as e:
            self.farmer.log.error(f"Error submitting partial to pool: {e}")
            return
        self.farmer.log.debug(f"Pool response: {submit_partial_response}")
        if "error_code" in submit_partial_response:
            if submit_partial_response["error_code"] == 5:
                self.farmer.log.info(
                    "Difficulty too low, adjusting to pool difficulty "
                    f"({submit_partial_response['current_difficulty']})"
                )
                self.farmer.pool_difficulty = submit_partial_response["current_difficulty"]
            else:
                self.farmer.log.error(
                    f"Error in pooling: {submit_partial_response['error_code'], submit_partial_response['error_message']}"
                )
        else:
            self.farmer.pool_difficulty = submit_partial_response["current_difficulty"]
