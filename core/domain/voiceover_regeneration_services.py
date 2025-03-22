# coding: utf-8
#
# Copyright 2024 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Service functions related to automatic voiceover regenration using the cloud
services.
"""

from __future__ import annotations

import html
import io
import json
import logging
import re
import uuid

from core import feconf
from core import utils
from core.domain import exp_fetchers
from core.domain import fs_services
from core.domain import state_domain
from core.domain import translation_fetchers
from core.domain import voiceover_services

from core.platform import models

import bs4
from mutagen import mp3
from pylatexenc import latex2text
from typing import Dict, List, Optional, Tuple, Union

MYPY = False
if MYPY: # pragma: no cover
    from mypy_imports import speech_synthesis_services
    from mypy_imports import voiceover_models

(voiceover_models,) = models.Registry.import_models([
    models.Names.VOICEOVER])

speech_synthesis_services = (
    models.Registry.import_azure_speech_synthesis_services())


ALLOWED_CUSTOM_OPPIA_RTE_TAGS = [
    'oppia-noninteractive-collapsible',
    'oppia-noninteractive-image',
    'oppia-noninteractive-link',
    'oppia-noninteractive-math',
    'oppia-noninteractive-video',
    'oppia-noninteractive-skillreview',
    'oppia-noninteractive-tabs'
]


def convert_custom_oppia_tags_to_generic_tags(element: bs4.Tag) -> bs4.Tag:
    """The method transforms custom Oppia tags into standard paragraph tags.

    Args:
        element: Tag. A custom Oppia tag that will be transformed into a
            standard paragraph tag.

    Returns:
        Tag. The transformed paragraph tag.
    """
    # The custom tags for images, videos, tabs, and collapsible
    # tags are not processed here because we do not plan to
    # provide voiceovers for the text associated with these tags.

    if element.name in [
        'oppia-noninteractive-link',
        'oppia-noninteractive-skillreview'
    ]:
        escaped_text = element.get('text-with-value')
        text = html.unescape(escaped_text)
        element.string = json.loads(text)
    elif element.name == 'oppia-noninteractive-math':
        escaped_math_content = element.get('math_content-with-value')
        math_content = json.loads(html.unescape(escaped_math_content))
        latex_expr = math_content['raw_latex']
        converter = latex2text.LatexNodes2Text()
        element.string = converter.latex_to_text(latex_expr)

    element.name = 'p'
    return element


def parse_html(html_content: str) -> str:
    """The method processes the HTML content and extracts the plain text.

    Args:
        html_content: str. HTML content that will be transformed into plain
            text.

    Returns:
        str. The plain text retrieved from the HTML content.
    """

    soup = bs4.BeautifulSoup(html_content, 'html.parser')
    for custom_tag_element in ALLOWED_CUSTOM_OPPIA_RTE_TAGS:
        for element in soup.find_all(custom_tag_element):
            convert_custom_oppia_tags_to_generic_tags(element)

    text_content: str = soup.get_text(
        separator=feconf.OPPIA_CONTENT_TAG_DELIMITER, strip=True)

    return text_content


def preprocess_text_for_voiceover_regeneration(text: str) -> str:
    """Preprocesses the input text for voiceover regeneration by applying
    custom rules to handle special cases.
    This ensures the text is modified appropriately for accurate voiceovers.

    Args:
        text: str. The text that will be preprocessed.

    Returns:
        str. The text after applying preprocessing rules.
    """
    # Replace multiple underscores with string `dash`.
    updated_text = re.sub(r'_{2,}', 'dash', text)

    # Replaces the division operator `/` surrounded by numerals and spaces
    # with the Unicode character `÷`.
    pattern = r'(\d+)\s*/\s*(\d+)'
    updated_text = re.sub(pattern, r'\1 ÷ \2', updated_text)

    return updated_text


def synthesize_voiceover_for_html_string(
    exploration_id: str,
    content_html: str,
    language_accent_code: str,
    voiceover_filename: str
) -> List[Dict[str, Union[str, float]]]:
    """The method generates automated voiceovers for the given HTML content
    using cloud service helper functions.

    Args:
        exploration_id: str. The exploration ID associated with the content.
        content_html: str. The HTML content string for which the automated
            voiceover is to be generated.
        language_accent_code: str. The language accent code for generating the
            automated voiceover.
        voiceover_filename: str. The filename for the generated voiceover.

    Returns:
        list(dict(str, str|float)). A list of dictionaries. Each dictionary
        contains two keys: 'token', which holds a string representing a word
        or punctionation from the content, and 'audio_offset_msecs', which
        stores a float value representing the associated time offset in the
        audio in msecs.
        Note: This field only contains the audio offset for automated
        voiceovers that are synthesized from using cloud service. These audio
        offsets are not provided or stored for manual voiceovers.

    Raises:
        Exception. Error encountered during automatic voiceover regeneration.
    """
    # Audio files are stored to the datastore in the dev env, and to GCS
    # in production.
    fs = fs_services.GcsFileSystem(
        feconf.ENTITY_TYPE_EXPLORATION, exploration_id)

    parsed_text = parse_html(content_html)
    processed_text_for_voiceover_regeneration = (
        preprocess_text_for_voiceover_regeneration(parsed_text))

    content_hash_code = (
        voiceover_models.CachedAutomaticVoiceoversModel.
        generate_hash_from_text(processed_text_for_voiceover_regeneration)
    )
    cached_model: Optional[voiceover_models.CachedAutomaticVoiceoversModel] = (
        voiceover_models.CachedAutomaticVoiceoversModel.
        get_cached_automatic_voiceover_model(
            content_hash_code,
            language_accent_code,
            feconf.OPPIA_AUTOMATIC_VOICEOVER_PROVIDER
        )
    )

    audio_offset_list: List[Dict[str, Union[str, float]]] = []

    is_voiceover_from_cache = False
    if cached_model is not None:
        error_details = None
        try:
            # If the content is available in the cache, use the cached
            # voiceovers.
            if (
                cached_model.plaintext ==
                processed_text_for_voiceover_regeneration
            ):
                audio_offset_list = (
                    cached_model.audio_offset_list)
                filename = cached_model.voiceover_filename
                binary_audio_data = fs.get('%s/%s' % ('audio', filename))
                is_voiceover_from_cache = True
        except Exception as e:
            cached_model = None
            logging.error('Failed to retrieve voiceover from cache: %s' % e)

    # Generate automatic voiceover only if the content is not available in the
    # cache; otherwise, use the cached voiceovers.
    if not is_voiceover_from_cache:
        try:
            binary_audio_data, audio_offset_list, error_details = (
                speech_synthesis_services.regenerate_speech_from_text(
                    processed_text_for_voiceover_regeneration,
                    language_accent_code)
            )
        except Exception as e:
            error_details = str(e)

    if error_details:
        raise Exception(error_details)

    tempbuffer = io.BytesIO()
    tempbuffer.write(binary_audio_data)
    tempbuffer.seek(0)
    audio = mp3.MP3(tempbuffer)
    tempbuffer.close()
    mimetype = 'audio/mpeg'
    # For a strange, unknown reason, the audio variable must be
    # deleted before opening cloud storage. If not, cloud storage
    # throws a very mysterious error that entails a mutagen
    # object being recursively passed around in app engine.
    del audio
    fs.commit(
        '%s/%s' % ('audio', voiceover_filename),
        binary_audio_data, mimetype=mimetype)

    # In case the content is not available in the cache, store the generated
    # voiceovers in the cache.
    if cached_model is not None:
        if cached_model.plaintext != processed_text_for_voiceover_regeneration:
            if (
                len(processed_text_for_voiceover_regeneration) <
                len(cached_model.plaintext)
            ):
                # Since the current text is shorter than the one in the cached
                # model, there is a higher likelihood of repetition in
                # other content. Thus, updating the cached model to store the
                # current data.
                cached_model.plaintext = (
                    processed_text_for_voiceover_regeneration)
                cached_model.voiceover_filename = voiceover_filename
                cached_model.audio_offset_list = audio_offset_list
                cached_model.update_timestamps()
                cached_model.put()
    else:
        new_cached_model = (
            voiceover_models.CachedAutomaticVoiceoversModel.create_cache_model(
                language_accent_code,
                processed_text_for_voiceover_regeneration,
                voiceover_filename,
                audio_offset_list))
        new_cached_model.update_timestamps()
        new_cached_model.put()

    return audio_offset_list


def generate_new_voiceover_filename(
        content_id: str, language_accent_code: str) -> str:
    """Generates a unique filename for a new voiceover. The filename is composed
    of the content ID, language accent code, and a random 10-character string.

    Args:
        content_id: str. The content ID for which the voiceover is generated.
        language_accent_code: str. The language accent code for the voiceover.

    Returns:
        str. The generated filename for the voiceover.
    """
    random_string_for_filename = utils.convert_to_hash(uuid.uuid4().hex, 10)
    return '%s-%s-%s.mp3' % (
        content_id,
        language_accent_code,
        random_string_for_filename
    )


def get_content_html_in_requested_language(
    exploration_id: str,
    exploration_version: int,
    state_name: str,
    content_id: str,
    language_accent_code: str
) -> str:
    """Fetches the content HTML in the requested language using the translation
    service.

    Args:
        exploration_id: str. The ID of the exploration.
        exploration_version: int. The version of the exploration.
        state_name: str. The name of the state.
        content_id: str. The content ID.
        language_accent_code: str. The language accent code.

    Returns:
        str. The content HTML in the requested language.

    Raises:
        Exception. The translation for the content ID is not found in the
            requested language.
    """
    language_code = (
        voiceover_services.
        get_language_code_from_language_accent_code(language_accent_code))
    assert isinstance(language_code, str)

    if language_code == 'en':
        exploration = exp_fetchers.get_exploration_by_id(exploration_id)
        content_html = exploration.get_content_html(state_name, content_id)
        assert isinstance(content_html, str)
        return content_html
    else:
        entity_translations = translation_fetchers.get_entity_translation(
            feconf.TranslatableEntityType(feconf.ENTITY_TYPE_EXPLORATION),
            exploration_id,
            exploration_version,
            language_code
        )
        try:
            translated_content_html = entity_translations.translations[
                content_id].content_value
            assert isinstance(translated_content_html, str)
        except Exception as e:
            raise Exception(
                'Translation for content_id %s not found in language %s' % (
                    content_id, language_code)) from e
        return translated_content_html


def regenerate_voiceover_for_exploration_content(
    exploration_id: str,
    exploration_version: int,
    state_name: str,
    content_id: str,
    language_accent_code: str
) -> Tuple[state_domain.Voiceover, List[Dict[str, Union[str, float]]]]:
    """Regenerates the voiceover for the given exploration content in the
    requested language accent code.

    Args:
        exploration_id: str. The ID of the exploration.
        exploration_version: int. The version of the exploration.
        state_name: str. The name of the state.
        content_id: str. The content ID.
        language_accent_code: str. The language accent code for the voiceover.

    Returns:
        tuple(Voiceover, list(dict(str, str|float))). A tuple containing the
        voiceover object and a list of dictionaries. The voiceover object
        contains the voiceover filename, audio size in bytes, duration in
        seconds, and whether the voiceover is needs update. The list of
        dictionaries contains the audio offset for each token in the content.
        Each dictionary contains two keys.
        - 'token': str. The token representing a word or punctuation in the
        content.
        - 'audio_offset_msecs': float. The time offset in milliseconds in the
        audio for the token.
        Note: This field only contains the audio offset for automated
        voiceovers that are synthesized from using cloud service.
    """
    content_html = get_content_html_in_requested_language(
        exploration_id,
        exploration_version,
        state_name,
        content_id,
        language_accent_code
    )
    voiceover_filename = generate_new_voiceover_filename(
        content_id, language_accent_code)

    sentence_tokens_with_durations = synthesize_voiceover_for_html_string(
        exploration_id, content_html, language_accent_code, voiceover_filename)

    fs = fs_services.GcsFileSystem(
        feconf.ENTITY_TYPE_EXPLORATION, exploration_id)

    binary_audio_data = fs.get('%s/%s' % ('audio', voiceover_filename))

    tempbuffer = io.BytesIO()
    tempbuffer.write(binary_audio_data)
    tempbuffer.seek(0)
    audio = mp3.MP3(tempbuffer)

    duration_secs = audio.info.length
    audio_size_bytes = tempbuffer.getbuffer().nbytes

    voiceover = state_domain.Voiceover(
        voiceover_filename, audio_size_bytes, False, duration_secs)

    entity_voiceovers = (
        voiceover_services.get_voiceovers_for_given_language_accent_code(
            feconf.ENTITY_TYPE_EXPLORATION,
            exploration_id,
            exploration_version,
            language_accent_code
        )
    )
    entity_voiceovers.add_voiceover(
        content_id, feconf.VoiceoverType.AUTO, voiceover)
    entity_voiceovers.add_automated_voiceovers_audio_offsets(
        content_id, sentence_tokens_with_durations)
    entity_voiceovers.validate()
    voiceover_services.save_entity_voiceovers(entity_voiceovers)

    return voiceover, sentence_tokens_with_durations
